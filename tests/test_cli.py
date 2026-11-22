"""CLI: the load command reports the run and exits non-zero on a rejected batch."""

from pathlib import Path

import duckdb

from supplygate.cli import main

SPEC = Path(__file__).resolve().parents[1] / "specs" / "dev" / "de_csv.yaml"


def test_generate_then_load_dev_feed(tmp_path, capsys):
    out, db = tmp_path / "gen", tmp_path / "sg.duckdb"
    assert main(["generate", "--seed", "3", "--out", str(out), "--supplier", "de_csv"]) == 0
    feed = out / "dev" / "de_csv" / "input" / "feed.csv"
    assert main(["load", "--spec", str(SPEC), "--feed", str(feed), "--db", str(db)]) == 0
    assert "(conserved)" in capsys.readouterr().out
    with duckdb.connect(str(db)) as con:
        assert con.execute("SELECT count(*) FROM load_runs WHERE status = 'committed'").fetchone()[0] == 1


def test_rejected_batch_exits_with_code_2(tmp_path, capsys):
    feed = tmp_path / "feed.csv"
    feed.write_text("sku,brand\nA,AMD\nB,Intel\n", encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "supplier: t\nsource: {format: csv}\n"
        "filter_rows: {keep_if: [{column: brand, op: eq, value: AMD}]}\n"
        "columns:\n  - {target: supplier_sku, source: sku}\n",
        encoding="utf-8",
    )
    assert main(["load", "--spec", str(spec), "--feed", str(feed), "--db", str(tmp_path / "sg.duckdb")]) == 2
    assert "missing 1: [2]" in capsys.readouterr().err
