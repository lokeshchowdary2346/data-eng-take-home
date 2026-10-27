-- Problem 02: user risk score calendar
-- Dialect: Redshift-compatible SQL, also runs on PostgreSQL 14+ (tested on 16).
--
-- Design
--   risk_score_intervals      one row per score change per user: the score holds from
--                             valid_from to valid_to (inclusive dates). Used for point lookups.
--   risk_score_daily_summary  one row per day: active_users and the average score.
--                             Used for the population average.
--
-- Assumptions
--   * The score on date D is the last score recorded on or before D, carried forward until
--     the user's next event.
--   * No score before a user's first event: the lookup returns no rows and the user is not
--     in that day's average.
--   * Dates are UTC (CAST(created_ts AS DATE)).
--   * Several events on one day: the latest created_ts wins. An exact timestamp tie takes the
--     higher score, just so rebuilds are deterministic.
--   * Lookups after CURRENT_DATE return nothing. Each user's last interval is open-ended
--     (valid_to = 9999-12-31), so nothing needs writing each day.
--   * Consecutive days with the same score are merged into one interval.
--
-- Why this shape
--   A dense user x day table grows with time even when nothing changes (5M users over 2 years
--   is roughly 3.6B rows). Querying the raw events means finding "latest row at or before D"
--   per user on every query. Intervals alone make the population average scan every interval
--   that overlaps the date. Intervals plus a small daily summary keeps storage at
--   O(score changes + days) and makes both lookups cheap.
--
-- Cost at millions of rows (N events, T days)
--   Storage  intervals <= N rows, summary = T rows.
--   Build    one partitioned sort of N rows for the intervals. The summary is a single pass
--            using a running sum of +score at valid_from and -score the day after valid_to,
--            which avoids a calendar x intervals range join.
--   Point    sorted on (user_id, valid_from), so it touches one user's few rows.
--   Average  reads one row by score_date.
--   Late-arriving data changes every day from its date onward, so rebuild the summary from
--   the earliest affected date.
--
-- Limits
--   The calendar generator covers 10,000 days from the first event. Scores are cast to
--   DECIMAL(38,10) for the running sum so repeated add/subtract doesn't drift, which assumes
--   at most 10 decimal places.
--
-- Redshift physical design (run these as the CREATE TABLEs, then INSERT ... SELECT with the
-- same query bodies; plain Postgres doesn't support DISTKEY/SORTKEY)
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
-- Note: "DATE + 1" and "DATE - 1" work in both Redshift and Postgres. I avoided recursive
-- CTEs because Redshift limits them to max_recursion_rows (default 100).


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
