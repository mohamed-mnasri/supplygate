import duckdb

import supplygate


def test_version():
    assert supplygate.__version__


def test_duckdb_available():
    con = duckdb.connect()
    assert con.execute("select 1").fetchone() == (1,)
