import duckdb

import supplygate


def test_version():
    assert supplygate.__version__


def test_duckdb_available():
    assert duckdb.sql("select 1").fetchone() == (1,)
