"""Tiny PostgreSQL helper: talks to the `psql` CLI, so no Python DB driver is needed.

Connection comes from the standard libpq env vars (PGHOST, PGPORT, PGUSER, PGPASSWORD).
"""
import os
import re
import subprocess
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_FILE = os.path.join(ROOT, "02.sql")
CSV_FILE = os.path.join(ROOT, "data", "user_risk_data.csv")


def psql(sql: str, dbname: str = "postgres") -> list[list[str]]:
    """Run SQL (multi-statement ok). Returns rows of the LAST result set as strings."""
    proc = subprocess.run(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", "-F", "|", "-d", dbname],
        input=sql, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed:\n{proc.stderr}\n--- SQL ---\n{sql[:800]}")
    return [line.split("|") for line in proc.stdout.splitlines() if line != ""]


def new_database() -> str:
    name = f"risk_test_{uuid.uuid4().hex[:8]}"
    psql(f'CREATE DATABASE "{name}"')
    return name


def drop_database(name: str) -> None:
    psql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def statements(path: str = SQL_FILE) -> list[str]:
    """Split the .sql file into statements (comment lines removed first)."""
    text = "\n".join(re.sub(r"--.*$", "", line) for line in open(path).read().splitlines())
    return [s.strip() for s in text.split(";") if s.strip()]


def build_statements() -> list[str]:
    return [s for s in statements() if re.match(r"(DROP|CREATE)\b", s, re.I)]


def query_statements() -> list[str]:
    return [s for s in statements() if re.match(r"SELECT\b", s, re.I)]


def load_source_table(db: str, df) -> None:
    """Create user_risk_scores in `db` from a DataFrame (user_id, created_ts, risk_score)."""
    values = ",".join(
        f"({int(r.user_id)}, TIMESTAMP '{r.created_ts:%Y-%m-%d %H:%M:%S}', {float(r.risk_score)!r})"
        for r in df.itertuples()
    )
    psql(
        "DROP TABLE IF EXISTS user_risk_scores;"
        "CREATE TABLE user_risk_scores (user_id INTEGER, created_ts TIMESTAMP, risk_score DOUBLE PRECISION);"
        f"INSERT INTO user_risk_scores VALUES {values};",
        db,
    )


def build_tables(db: str) -> None:
    psql(";\n".join(build_statements()) + ";", db)
