"""Independent brute-force oracle (pandas forward-fill), deliberately NOT the SQL's logic."""
import pandas as pd


def expected_daily_scores(df: pd.DataFrame, today) -> pd.DataFrame:
    """Dense (user_id, day, risk_score): last score of each day, carried forward to `today`."""
    d = df.assign(day=df.created_ts.dt.normalize())
    d = d.sort_values(["user_id", "created_ts", "risk_score"])  # same-ts tie -> higher score last
    last = d.groupby(["user_id", "day"], as_index=False).tail(1)
    frames = []
    for uid, g in last.groupby("user_id"):
        s = g.set_index("day")["risk_score"]
        idx = pd.date_range(s.index.min(), pd.Timestamp(today))
        out = s.reindex(idx).ffill().rename_axis("day").reset_index()
        out["user_id"] = uid
        frames.append(out)
    return pd.concat(frames, ignore_index=True)[["user_id", "day", "risk_score"]]


def expected_population(dense: pd.DataFrame) -> pd.DataFrame:
    g = dense.groupby("day")["risk_score"].agg(avg="mean", users="count").reset_index()
    return g
