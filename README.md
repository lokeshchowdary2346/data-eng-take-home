# Data engineering take-home

Two problems from a data engineering take-home.

- `01.py`: in-memory store for user purchases. Run it with `python 01.py`.
- `02.sql`: SQL for tracking each user's risk score over time. Written for Redshift, also runs on Postgres.
- `data/user_risk_data.csv`: sample data for problem 02.
- `tests/test_01.py`: tests for problem 01 (`pip install pytest`, then `pytest`).

## Problem 01

Purchases are stored in a dict keyed by `user_id`, with a second dict from product to the set of
users who bought it, so both lookups are a single dict access. Money is kept in cents. Notes on the
design and on the oddities in the sample data (a purchase with no matching user, duplicate usernames)
are at the top of `01.py`.

## Problem 02

Two tables: one row per score change per user (`risk_score_intervals`), and one row per day with the
average score (`risk_score_daily_summary`). That avoids a user x day table that keeps growing even
when nothing changes. Assumptions and the cost at larger sizes are in the header of `02.sql`.

To try it, load the CSV into a table called `user_risk_scores` and run `02.sql`:

```sql
CREATE TABLE user_risk_scores (user_id INT, created_ts TIMESTAMP, risk_score DOUBLE PRECISION);
\copy user_risk_scores FROM 'data/user_risk_data.csv' CSV HEADER
\i 02.sql
```
