"""Tests for solution/02.sql against PostgreSQL, checked with an independent pandas oracle."""
import datetime as dt
import math
import random

import pandas as pd
import pytest

import pg
from oracle import expected_daily_scores, expected_population


def point_query(user_id: int, day) -> str:
    sql = pg.query_statements()[0]
    return sql.replace("1002", str(user_id)).replace("2025-03-05", str(day))


def population_query(day, which: int = 1) -> str:
    sql = pg.query_statements()[which]
    return sql.replace("2025-03-05", str(day))


def lookup(db, user_id, day):
    rows = pg.psql(point_query(user_id, day), db)
    assert len(rows) <= 1, "point lookup must never return more than one row"
    return float(rows[0][0]) if rows else None


# ---------------------------------------------------------------- structure
def test_intervals_are_contiguous_non_overlapping_and_open_ended(db):
    rows = pg.psql(
        "SELECT user_id, valid_from, valid_to FROM risk_score_intervals ORDER BY user_id, valid_from", db
    )
    by_user = {}
    for uid, f, t in rows:
        by_user.setdefault(int(uid), []).append((dt.date.fromisoformat(f), dt.date.fromisoformat(t)))
    for uid, spans in by_user.items():
        for (f1, t1), (f2, _) in zip(spans, spans[1:]):
            assert f1 <= t1
            assert f2 == t1 + dt.timedelta(days=1), f"user {uid}: gap or overlap"
        assert spans[-1][1] == dt.date(9999, 12, 31), f"user {uid}: last interval must be open"


def test_storage_is_smaller_than_events_and_far_smaller_than_dense(db, sample_df, today):
    n_int = int(pg.psql("SELECT COUNT(*) FROM risk_score_intervals", db)[0][0])
    dense = len(expected_daily_scores(sample_df, today))
    assert n_int <= len(sample_df)
    assert n_int < dense / 10


def test_equal_consecutive_scores_are_merged(db):
    # user 1028 repeats 0.3537 four times then changes once -> exactly 2 intervals
    rows = pg.psql("SELECT valid_from, valid_to, risk_score FROM risk_score_intervals WHERE user_id=1028 ORDER BY 1", db)
    assert [(r[0], float(r[2])) for r in rows] == [("2025-01-09", 0.3537), ("2025-02-14", 0.9762)]
    assert rows[0][1] == "2025-02-13"


# ------------------------------------------------- point lookup correctness
def test_every_user_every_day_matches_oracle(db, sample_df, today):
    """Expand the intervals table to user x day inside Postgres and compare with the oracle."""
    rows = pg.psql(
        "SELECT i.user_id, d::date, i.risk_score "
        "FROM risk_score_intervals i "
        "JOIN LATERAL generate_series(i.valid_from, LEAST(i.valid_to, CURRENT_DATE), interval '1 day') d ON TRUE",
        db,
    )
    got = pd.DataFrame(rows, columns=["user_id", "day", "risk_score"]).astype(
        {"user_id": int, "risk_score": float}
    )
    got["day"] = pd.to_datetime(got["day"])
    want = expected_daily_scores(sample_df, today)
    merged = want.merge(got, on=["user_id", "day"], how="outer", suffixes=("_want", "_got"), indicator=True)
    assert (merged["_merge"] == "both").all(), merged[merged["_merge"] != "both"].head()
    assert (merged.risk_score_want - merged.risk_score_got).abs().max() < 1e-12


def test_point_query_text_matches_oracle_on_random_and_boundary_pairs(db, sample_df, today):
    want = expected_daily_scores(sample_df, today)
    lookup_map = {(r.user_id, r.day.date()): r.risk_score for r in want.itertuples()}
    first = want.groupby("user_id")["day"].min()
    rng = random.Random(7)
    pairs = [(int(u), rng.choice([k[1] for k in lookup_map if k[0] == u])) for u in rng.sample(sorted(first.index), 40)]
    for u, d0 in first.items():  # day before / first day for a few users
        if u in (1000, 1046, 1053):
            pairs += [(int(u), d0.date() - dt.timedelta(days=1)), (int(u), d0.date())]
    for uid, day in pairs:
        expected = lookup_map.get((uid, day))
        got = lookup(db, uid, day)
        if expected is None:
            assert got is None, (uid, day)
        else:
            assert got is not None and math.isclose(got, expected, abs_tol=1e-12), (uid, day, got, expected)


def test_before_first_event_and_future_and_unknown_user_return_no_rows(db, today):
    assert lookup(db, 1000, "2025-01-06") is None            # day before first event (2025-01-07)
    assert lookup(db, 1000, "2025-01-07") == 0.2449          # first-event day
    assert lookup(db, 1000, today + dt.timedelta(days=1)) is None
    assert lookup(db, 999999, "2025-03-05") is None


def test_today_returns_latest_score(db, today):
    assert lookup(db, 1000, today) == 0.1025


def test_multiple_events_same_day_last_one_wins(db):
    # user 1046, 2025-01-26: 12:08 -> .1918, 16:42 -> .9404, 19:43 -> .7481
    assert lookup(db, 1046, "2025-01-26") == 0.7481
    assert lookup(db, 1046, "2025-02-22") == 0.7481          # carried forward until next event
    assert lookup(db, 1046, "2025-02-23") == 0.3009


def test_long_gap_is_carried_forward(db):
    assert lookup(db, 1053, "2025-10-21") == 0.2773          # event Jan 16 then Oct 22
    assert lookup(db, 1053, "2025-10-22") == 0.5213


# -------------------------------------------------- population aggregate
def test_summary_has_one_row_per_day_first_event_to_today(db, today):
    n, lo, hi = pg.psql("SELECT COUNT(*), MIN(score_date), MAX(score_date) FROM risk_score_daily_summary", db)[0]
    assert lo == "2025-01-06" and dt.date.fromisoformat(hi) == today
    assert int(n) == (today - dt.date(2025, 1, 6)).days + 1


def test_summary_matches_oracle_for_every_date(db, sample_df, today):
    want = expected_population(expected_daily_scores(sample_df, today)).set_index("day")
    rows = pg.psql("SELECT score_date, active_users, avg_risk_score FROM risk_score_daily_summary", db)
    assert len(rows) == len(want)
    for d, n, avg in rows:
        w = want.loc[pd.Timestamp(d)]
        assert int(n) == w.users, d
        assert math.isclose(float(avg), w.avg, abs_tol=1e-9), (d, avg, w.avg)


def test_population_queries_agree_with_each_other_and_oracle(db, sample_df, today):
    want = expected_population(expected_daily_scores(sample_df, today)).set_index("day")
    for d in ["2025-01-06", "2025-01-31", "2025-03-05", "2025-04-30", "2025-10-22", "2026-01-01"]:
        s = pg.psql(population_query(d, 1), db)[0]
        f = pg.psql(population_query(d, 2), db)[0]
        w = want.loc[pd.Timestamp(d)]
        assert math.isclose(float(s[2]), w.avg, abs_tol=1e-9)
        assert math.isclose(float(f[0]), w.avg, abs_tol=1e-9)
        assert int(s[1]) == int(f[1]) == w.users


def test_population_average_before_any_data_returns_no_rows(db):
    assert pg.psql(population_query("2024-12-31", 1), db) == []


# --------------------------------------------------------- re-runs & edge data
def test_rebuild_is_idempotent(db):
    q = ("SELECT md5(string_agg(user_id||'|'||valid_from||'|'||valid_to||'|'||risk_score, ',' "
         "ORDER BY user_id, valid_from)) FROM risk_score_intervals")
    before = pg.psql(q, db)
    pg.build_tables(db)
    assert pg.psql(q, db) == before


def test_synthetic_edge_cases(scratch_db):
    ts = pd.Timestamp
    df = pd.DataFrame([
        # A -> B -> A must not be merged into one interval
        (1, ts("2025-03-01 09:00"), 0.2), (1, ts("2025-03-03 09:00"), 0.7), (1, ts("2025-03-05 09:00"), 0.2),
        # same-timestamp tie: higher score wins deterministically
        (2, ts("2025-03-02 10:00"), 0.3), (2, ts("2025-03-02 10:00"), 0.9),
        # midnight boundary: 23:59:59 belongs to the earlier day, 00:00:00 to the next
        (3, ts("2025-03-01 23:59:59"), 0.4), (3, ts("2025-03-02 00:00:00"), 0.6),
        # single event, scores at the range limits
        (4, ts("2025-03-04 12:00"), 0.0), (5, ts("2025-03-04 12:00"), 1.0),
    ], columns=["user_id", "created_ts", "risk_score"])
    db = scratch_db(df)
    today = dt.date.fromisoformat(pg.psql("SELECT CURRENT_DATE", db)[0][0])
    want = expected_daily_scores(df, today)
    want_map = {(r.user_id, r.day.date()): r.risk_score for r in want.itertuples()}

    assert int(pg.psql("SELECT COUNT(*) FROM risk_score_intervals WHERE user_id=1", db)[0][0]) == 3
    for (uid, day), score in list(want_map.items())[:400:7] + [((3, dt.date(2025, 3, 1)), 0.4), ((3, dt.date(2025, 3, 2)), 0.6)]:
        got = lookup(db, uid, day)
        assert got is not None and math.isclose(got, want_map[(uid, day)], abs_tol=1e-12), (uid, day)
    assert lookup(db, 2, "2025-03-02") == 0.9
    assert lookup(db, 4, "2025-03-04") == 0.0 and lookup(db, 5, "2025-03-04") == 1.0

    pop = expected_population(want).set_index("day")
    for d, n, avg in pg.psql("SELECT score_date, active_users, avg_risk_score FROM risk_score_daily_summary", db):
        w = pop.loc[pd.Timestamp(d)]
        assert int(n) == w.users and math.isclose(float(avg), w.avg, abs_tol=1e-9), d
