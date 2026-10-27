# Data engineering take-home

Solutions to a two-part data engineering take-home: an in-memory data structure for
user purchases (Python) and a SQL design for tracking user risk scores over time.

| File | What it is |
| --- | --- |
| `01.py` | Problem 01, in-memory user sales store. Standard library only. |
| `02.sql` | Problem 02, risk score calendar. Postgres / Redshift-compatible SQL. |
| `tests/` | pytest suites for both problems. |
| `data/user_risk_data.csv` | Sample data supplied with the problem. |

The design reasoning, assumptions and edge cases are written up in the docstring at the top of
`01.py` and the comment block at the top of `02.sql`, so each file stands on its own.

## Problem 01: user sales store

`SalesStore` keeps transactions in a dict keyed by `user_id` (the access path the problem calls out),
plus a product-to-buyers index so "who bought product X" doesn't need a scan. Money is held as integer
cents. Lifetime value is a running total, which is safe because the store is append-only (refunds are
negative transactions).

```bash
python3 01.py
```

Things the sample data forced me to decide:

- Purchases for a `user_id` with no user record (123456) are kept and flagged, not dropped.
- `username` isn't unique (two "andrew"s), so results are keyed by `user_id`.
- Costs come in as strings like `"$100.0"`; they are parsed to cents, and sub-cent values are rejected.

## Problem 02: risk score calendar

Two tables built from `user_risk_scores`:

- `risk_score_intervals`: one row per score *change* per user, as a closed date range. Used for the
  point lookup.
- `risk_score_daily_summary`: one row per day with the active user count and average score. Used for the
  population average.

A dense user x day table would grow with time even when nothing changes, so I avoided it. The comment
block in `02.sql` compares the alternatives and covers cost at millions of rows.

Assumptions worth knowing: a score is carried forward until the user's next event; the latest event of a
day wins; dates are UTC; a user is not in the population average before their first event.

## Running the tests

```bash
pip install -r requirements-dev.txt
make test        # everything
make test-01     # Problem 01 only, no database needed
```

`02.sql` is tested against PostgreSQL 16 (closest free stand-in for Redshift), driven through the `psql`
CLI. `scripts/run_tests.sh` uses whichever of these it finds first: a server you point at with
`PGHOST`/`PGPORT`/`PGUSER`, Docker (`docker-compose.yml`), or locally installed Postgres binaries.

The expected results are computed independently in pandas (`tests/oracle.py`) rather than by re-using the
SQL logic, so the tests can actually catch mistakes in it.

## Submission

`make package` zips `01.py` and `02.sql`.
