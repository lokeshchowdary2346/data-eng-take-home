-- =====================================================================================
-- Problem 02: User Risk Score Calendar
-- Dialect: Redshift-compatible SQL (also runs unchanged on PostgreSQL 14+).
--          Tested on PostgreSQL 16. Redshift-only physical options are shown in the
--          commented block just below (DISTKEY/SORTKEY cannot be run on plain Postgres).
-- =====================================================================================
--
-- DESIGN IN ONE PARAGRAPH
--   Two small tables built from user_risk_scores:
--     1) risk_score_intervals      one row per (user, score CHANGE): the score holds from
--                                  valid_from through valid_to (both inclusive dates).
--                                  Source of truth for point lookups.
--     2) risk_score_daily_summary  one row per calendar day: active_users and the average
--                                  score across them. Answers the analyst question directly.
--   Neither table has a row per (user, day).
--
-- SEMANTICS (assumptions I am making explicit)
--   * "Score on date D" = the last score recorded at or before the end of day D, carried
--     forward until the user's next event (a score is a state, not a one-day event).
--   * A user has no score before the date of their first event. Point lookup returns zero
--     rows there, and the user is NOT in that day's population average.
--   * Dates are UTC calendar dates, CAST(created_ts AS DATE). If the business day is in
--     another time zone, convert created_ts before the cast.
--   * Several events on one day: the latest created_ts wins. If two rows share the exact
--     same created_ts for one user, the higher risk_score wins (arbitrary but deterministic,
--     so rebuilds give identical results).
--   * Lookups beyond CURRENT_DATE return nothing (the requirement is first event through
--     the current date). The last interval of a user is open-ended: valid_to = 9999-12-31.
--     That means no nightly writes are needed to "extend" scores through today.
--   * If the score does not change from one event to the next (the sample has users
--     1028 to 1033 repeating a score) the days are merged into one interval.
--
-- WHY INTERVALS + A DAILY SUMMARY (and not the alternatives)
--   A) Dense user x day table. Simplest to query, but storage is users x days. 5M users
--      over 2 years is about 3.6 billion rows, and every day appends one row per user even
--      if nothing changed. Rejected: cost grows with time, not with information.
--   B) Raw events only (no new table). Free to build, but both queries need
--      "latest row at or before D" per user, which is a window or lateral join over a huge
--      table. The population average re-does that for every user on every analyst query.
--   C) Intervals only. Compact and good for point lookups; but the population average must
--      scan every interval overlapping D (it is O(intervals) per query).
--   D) Intervals + daily summary (chosen). Storage is O(score changes + days). Point
--      lookup is a prefix-sorted range probe on one user. The analyst query reads one row
--      from a table with one row per day. The only cost is a rebuild step, shown below.
--
-- PERFORMANCE AT MILLIONS OF ROWS (N = events, U = users, T = days)
--   Storage   intervals <= N rows (usually far fewer after merging equal scores and
--             same-day events). summary = T rows (a few hundred to a few thousand).
--   Build     intervals: one partitioned sort of N rows. Both window functions share the
--             partition key user_id, so with DISTKEY(user_id) there is no redistribution.
--             summary: one pass over the intervals using a running sum of +score at
--             valid_from and -score at the day after valid_to. It avoids a calendar x
--             intervals range join (O(T x intervals)). Cost is O(intervals + T).
--             Incrementally, only users with new events change; the summary is append-only
--             unless data arrives late (late data touches the days from its date forward,
--             so rebuild from the earliest affected date).
--   Point     user_id is the leading sort key: zone maps prune to a few blocks, then the
--             user's own intervals (a handful of rows) are filtered by date. Effectively
--             O(log) block probe + O(intervals per user).
--   Average   a single-row read from risk_score_daily_summary by score_date.
--
-- EDGE CASES HANDLED
--   user with 1 event; several events in one day; consecutive equal scores; score gap of
--   months (user 1053: Jan to Oct); date before first event; date after today; users that
--   start on different days (the population average denominator changes, active_users is
--   stored next to the average so analysts can see it); idempotent rebuild (DROP + CREATE).
--   NOT handled / known limits: the calendar generator below covers 10,000 days (about
--   27 years) from the earliest event (raise it by adding one more digits join); scores are
--   cast to DECIMAL(38,10) for the running sum so repeated add/subtract does not drift, which
--   assumes at most 10 decimal places of precision in risk_score.
--
-- REDSHIFT PHYSICAL DESIGN (apply by creating the table first, then INSERT ... SELECT with
-- the same query body; plain Postgres ignores/does not support these options)
--   CREATE TABLE risk_score_intervals (
--       user_id    INTEGER          NOT NULL,
--       valid_from DATE             NOT NULL,
--       valid_to   DATE             NOT NULL,
--       risk_score DOUBLE PRECISION NOT NULL
--   ) DISTKEY (user_id) SORTKEY (user_id, valid_from);
--   CREATE TABLE risk_score_daily_summary (
--       score_date     DATE             NOT NULL,
--       active_users   BIGINT           NOT NULL,
--       avg_risk_score DOUBLE PRECISION
--   ) DISTSTYLE ALL SORTKEY (score_date);
--
-- NOTE ON DATE ARITHMETIC: "DATE - 1" and "DATE + 1" return a DATE in both Redshift and
-- PostgreSQL. Recursive CTEs are avoided on purpose: Redshift caps them at
-- max_recursion_rows (default 100), which is too small for a daily calendar.
-- =====================================================================================


-- -------------------------------------------------------------------------------------
-- BUILD (idempotent: safe to re-run)
-- -------------------------------------------------------------------------------------
DROP TABLE IF EXISTS risk_score_daily_summary;
DROP TABLE IF EXISTS risk_score_intervals;

-- Table 1: one row per score change per user, as closed date intervals.
CREATE TABLE risk_score_intervals AS
WITH daily_last AS (
    -- Collapse to one row per user per day: the latest event of the day wins.
    SELECT user_id, score_date, risk_score
    FROM (
        SELECT user_id,
               CAST(created_ts AS DATE) AS score_date,
               risk_score,
               ROW_NUMBER() OVER (
                   PARTITION BY user_id, CAST(created_ts AS DATE)
                   ORDER BY created_ts DESC, risk_score DESC
               ) AS rn
        FROM user_risk_scores
    ) ranked
    WHERE rn = 1
),
changes AS (
    -- Keep only days where the score differs from the previous day-with-data.
    SELECT user_id, score_date, risk_score
    FROM (
        SELECT user_id, score_date, risk_score,
               LAG(risk_score) OVER (PARTITION BY user_id ORDER BY score_date) AS prev_score
        FROM daily_last
    ) lagged
    WHERE prev_score IS NULL OR prev_score <> risk_score
)
SELECT user_id,
       score_date AS valid_from,
       -- Day before the next change; the latest interval stays open-ended.
       COALESCE(
           LEAD(score_date) OVER (PARTITION BY user_id ORDER BY score_date) - 1,
           DATE '9999-12-31'
       ) AS valid_to,
       risk_score
FROM changes;

-- Table 2: one row per calendar day with the population average.
CREATE TABLE risk_score_daily_summary AS
WITH digits AS (
    SELECT 0 AS n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
    UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9
),
bounds AS (
    SELECT MIN(valid_from) AS first_date FROM risk_score_intervals
),
calendar AS (
    -- first event date through today (up to 10,000 days), no recursion
    SELECT b.first_date + (d1.n + 10 * d2.n + 100 * d3.n + 1000 * d4.n) AS score_date
    FROM bounds b
    CROSS JOIN digits d1 CROSS JOIN digits d2 CROSS JOIN digits d3 CROSS JOIN digits d4
    WHERE b.first_date + (d1.n + 10 * d2.n + 100 * d3.n + 1000 * d4.n) <= CURRENT_DATE
),
deltas AS (
    -- Sweep line: +score when an interval starts, -score the day after it ends.
    SELECT valid_from AS score_date,
           CAST(risk_score AS DECIMAL(38, 10)) AS delta_sum,
           1 AS delta_cnt
    FROM risk_score_intervals
    UNION ALL
    SELECT valid_to + 1,
           -CAST(risk_score AS DECIMAL(38, 10)),
           -1
    FROM risk_score_intervals
    WHERE valid_to < DATE '9999-12-31'
),
daily_deltas AS (
    SELECT score_date, SUM(delta_sum) AS delta_sum, SUM(delta_cnt) AS delta_cnt
    FROM deltas
    GROUP BY score_date
),
running AS (
    SELECT c.score_date,
           SUM(COALESCE(d.delta_sum, 0)) OVER (ORDER BY c.score_date ROWS UNBOUNDED PRECEDING) AS total_score,
           SUM(COALESCE(d.delta_cnt, 0)) OVER (ORDER BY c.score_date ROWS UNBOUNDED PRECEDING) AS active_users
    FROM calendar c
    LEFT JOIN daily_deltas d ON d.score_date = c.score_date
)
SELECT score_date,
       CAST(active_users AS BIGINT) AS active_users,
       CAST(total_score / NULLIF(active_users, 0) AS DOUBLE PRECISION) AS avg_risk_score
FROM running;


-- -------------------------------------------------------------------------------------
-- ACCESS QUERIES (sample literals: user 1002, date 2025-03-05; replace as needed)
-- -------------------------------------------------------------------------------------

-- Need 1: point lookup, risk score for a user on a date.
-- Zero rows means: before the user's first event, after today, or unknown user.
SELECT risk_score
FROM risk_score_intervals
WHERE user_id = 1002
  AND DATE '2025-03-05' BETWEEN valid_from AND valid_to
  AND DATE '2025-03-05' <= CURRENT_DATE;

-- Need 2: population average on a date (reads one row).
SELECT score_date, active_users, avg_risk_score
FROM risk_score_daily_summary
WHERE score_date = DATE '2025-03-05';

-- Need 2, fallback: the same answer computed straight from the intervals table, without the
-- summary table. Useful to cross-check the summary, or if the summary is not built yet.
SELECT AVG(risk_score) AS avg_risk_score, COUNT(*) AS active_users
FROM risk_score_intervals
WHERE DATE '2025-03-05' BETWEEN valid_from AND valid_to
  AND DATE '2025-03-05' <= CURRENT_DATE;
