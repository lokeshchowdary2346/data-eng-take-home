import datetime as dt
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import pg  # noqa: E402


@pytest.fixture(scope="session")
def sample_df():
    df = pd.read_csv(pg.CSV_FILE, parse_dates=["created_ts"])
    return df


@pytest.fixture(scope="session")
def db(sample_df):
    """A throwaway database with the sample CSV loaded and 02.sql built."""
    name = pg.new_database()
    try:
        pg.load_source_table(name, sample_df)
        pg.build_tables(name)
        yield name
    finally:
        pg.drop_database(name)


@pytest.fixture(scope="session")
def today(db) -> dt.date:
    return dt.date.fromisoformat(pg.psql("SELECT CURRENT_DATE", db)[0][0])


@pytest.fixture()
def scratch_db():
    """Factory: build a fresh DB from a custom DataFrame, return (db_name, today)."""
    made = []

    def make(df):
        name = pg.new_database()
        made.append(name)
        pg.load_source_table(name, df)
        pg.build_tables(name)
        return name

    yield make
    for n in made:
        pg.drop_database(n)
