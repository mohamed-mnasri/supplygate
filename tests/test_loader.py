"""Loader: quarantine, conservation, lineage and all-or-nothing runs."""

import csv
import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from supplygate.catalog import load_catalog
from supplygate.gates.conservation import ConservationError
from supplygate.loader import load_feed
from supplygate.mapping import load_spec
from supplygate.mapping.compiler import MissingSourceColumns
from supplygate.mapping.spec import parse_spec

SPECS = Path(__file__).resolve().parents[1] / "specs"
DEV_SUPPLIERS = ["de_csv", "fr_csv", "uk_csv"]

GPUS = [p for p in load_catalog() if p.category == "gpu"]

# A minimal supplier feed in canonical units, so each test controls exactly one defect.
SPEC = """
supplier: mini
source: {{format: csv, delimiter: ","}}
{filter}
columns:
  - {{target: supplier_id, const: {supplier}}}
  - {{target: supplier_sku, source: sku}}
  - {{target: category, const: gpu}}
  - {{target: brand, source: brand}}
  - {{target: model_name, source: model}}
  - {{target: gtin, source: gtin{gtin_transforms}}}
  - {{target: price_eur, source: price, transforms: [{{parse_number: {{decimal: "."}}}}{price_transforms}]}}
  - {{target: stock_qty, source: stock}}
  - {{target: gpu_vram_gb, source: vram}}
"""


def mini_spec(*, supplier="mini", filter="", gtin_transforms="", price_transforms=""):
    return parse_spec(SPEC.format(supplier=supplier, filter=filter, gtin_transforms=gtin_transforms, price_transforms=price_transforms))


def write_feed(path: Path, overrides: dict[int, dict] | None = None, n: int = 4) -> Path:
    """n clean GPU offers; ``overrides`` replaces cells of a given 1-based row."""
    rows = []
    for i, p in enumerate(GPUS[:n], start=1):
        row = {
            "sku": f"G{i}",
            "brand": p.brand,
            "model": p.model_name,
            "gtin": p.gtin,
            "price": str(p.reference_price_eur),
            "stock": "4",
            "vram": str(p.attributes["gpu_vram_gb"]),
        }
        row.update((overrides or {}).get(i, {}))
        rows.append(row)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


@pytest.fixture
def con():
    c = duckdb.connect()
    yield c
    c.close()


def quarantine(con, run_id) -> dict[int, tuple[list[str], list[str]]]:
    rows = con.execute("SELECT source_row, parse_errors, violations FROM quarantine WHERE run_id = ?", [run_id]).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


# --------------------------------------------------------------------------- end to end


@pytest.mark.parametrize("supplier", DEV_SUPPLIERS)
def test_dev_feed_quarantines_exactly_the_rows_ground_truth_expects(generated, con, supplier):
    out, _ = generated
    base = out / "dev" / supplier
    feed = next((base / "input").iterdir())
    spec = load_spec(SPECS / "dev" / f"{supplier}.yaml")

    report = load_feed(con, spec, feed)

    with open(base / "truth" / "ground_truth.csv", encoding="utf-8", newline="") as f:
        truth = list(csv.DictReader(f))
    expected = {int(r["source_row"]) for r in truth if r["expected_outcome"] == "quarantine"}
    assert set(quarantine(con, report.run_id)) == expected
    assert report.conservation.ok
    assert (report.rows_in, report.loaded, report.quarantined) == (len(truth), len(truth) - len(expected), len(expected))

    # every loaded offer traces back to its source row
    lineage = dict(con.execute("SELECT supplier_sku, source_row FROM offer_lineage WHERE run_id = ?", [report.run_id]).fetchall())
    truth_sku = {int(r["source_row"]): r["supplier_sku"] for r in truth}
    assert all(truth_sku[row] == sku for sku, row in lineage.items())
    assert len(lineage) == report.loaded


def test_run_record_identifies_feed_and_spec(tmp_path, con):
    feed = write_feed(tmp_path / "feed.csv")
    spec = mini_spec()
    report = load_feed(con, spec, feed)

    status, source_sha, spec_sha, spec_yaml, counts = con.execute(
        "SELECT status, source_sha256, spec_sha256, spec_yaml, [rows_in, rows_loaded, rows_quarantined] FROM load_runs WHERE run_id = ?",
        [report.run_id],
    ).fetchone()
    assert status == "committed"
    assert source_sha == hashlib.sha256(feed.read_bytes()).hexdigest()
    assert spec_sha == hashlib.sha256(spec.to_yaml().encode()).hexdigest() and spec_yaml == spec.to_yaml()
    assert counts == [4, 4, 0]


def test_column_lineage_records_sources_constants_and_transforms(tmp_path, con):
    report = load_feed(con, mini_spec(), write_feed(tmp_path / "feed.csv"))
    rows = con.execute(
        "SELECT target_column, source_columns, constant, transforms FROM column_lineage WHERE run_id = ?", [report.run_id]
    ).fetchall()
    by_target = {r[0]: r[1:] for r in rows}
    assert by_target["price_eur"] == (["price"], None, ["parse_number"])
    assert by_target["category"] == ([], "gpu", [])


# --------------------------------------------------------------------------- quarantine


def test_parse_errors_and_oracle_violations_are_kept_apart(tmp_path, con):
    feed = write_feed(tmp_path / "feed.csv", {2: {"price": "ab,c"}, 3: {"vram": "7"}})
    report = load_feed(con, mini_spec(), feed)
    q = quarantine(con, report.run_id)
    # the unparseable price becomes NULL, so required:price_eur fires as well
    assert q == {2: (["price_eur:parse_number"], ["required:price_eur"]), 3: ([], ["gpu_vram_size"])}
    assert report.reasons["parse:price_eur:parse_number"] == 1 and report.reasons["gpu_vram_size"] == 1


def test_quarantine_keeps_the_mapped_record(tmp_path, con):
    report = load_feed(con, mini_spec(), write_feed(tmp_path / "feed.csv", {1: {"stock": "-2"}}))
    sku, record = con.execute("SELECT supplier_sku, record FROM quarantine WHERE run_id = ?", [report.run_id]).fetchone()
    assert sku == "G1" and json.loads(record)["stock_qty"] == -2


# --------------------------------------------------------------------------- risky spec operations


def test_default_value_cannot_paper_over_a_missing_price(tmp_path, con):
    feed = write_feed(tmp_path / "feed.csv", {1: {"price": ""}})
    report = load_feed(con, mini_spec(price_transforms=", {default_value: 0}"), feed)
    assert report.risky_ops == ("default_value@price_eur",)
    assert quarantine(con, report.run_id) == {1: ([], ["price_range"])}


def test_null_if_cannot_hide_a_bad_gtin(tmp_path, con):
    bad = GPUS[0].gtin[:12] + str((int(GPUS[0].gtin[12]) + 1) % 10)
    feed = write_feed(tmp_path / "feed.csv", {1: {"gtin": bad}})
    report = load_feed(con, mini_spec(gtin_transforms=f', transforms: [{{null_if: "{bad}"}}]'), feed)
    assert quarantine(con, report.run_id) == {1: ([], ["required:gtin"])}


def test_spec_cannot_load_rows_for_another_supplier(tmp_path, con):
    report = load_feed(con, mini_spec(supplier="other_sup"), write_feed(tmp_path / "feed.csv", n=2))
    assert report.loaded == 0
    assert all(v == ([], ["supplier_mismatch"]) for v in quarantine(con, report.run_id).values())


def test_filter_rows_that_drops_records_rejects_the_whole_run(tmp_path, con):
    feed = write_feed(tmp_path / "feed.csv")
    first = load_feed(con, mini_spec(), feed)

    write_feed(feed, {3: {"stock": "-1"}})
    dropping = mini_spec(filter="filter_rows: {keep_if: [{column: stock, op: ge, value: 0}]}")
    with pytest.raises(ConservationError) as exc:
        load_feed(con, dropping, feed)
    assert exc.value.result.missing == (3,)

    # the catalog still holds the first run's offers, untouched
    assert con.execute("SELECT count(*) FROM canonical_offers").fetchone()[0] == 4
    assert con.execute("SELECT DISTINCT run_id FROM offer_lineage").fetchall() == [(first.run_id,)]
    # the rejected run is on record, with nothing else from it
    runs = con.execute("SELECT status, conservation FROM load_runs ORDER BY started_at").fetchall()
    assert [r[0] for r in runs] == ["committed", "rejected"] and "missing 1: [3]" in runs[1][1]
    assert con.execute("SELECT count(*) FROM quarantine").fetchone()[0] == 0


def test_spec_reading_the_header_as_data_is_rejected(tmp_path, con):
    feed = write_feed(tmp_path / "feed.csv", n=2)
    spec = parse_spec(mini_spec().to_yaml().replace("format: csv", "format: csv\n  header: false"))
    with pytest.raises((ConservationError, MissingSourceColumns)):
        load_feed(con, spec, feed)


# --------------------------------------------------------------------------- re-runs and failures


def test_reloading_a_supplier_replaces_its_offers(tmp_path, con):
    feed = write_feed(tmp_path / "feed.csv")
    load_feed(con, mini_spec(), feed)
    write_feed(feed, n=3)
    second = load_feed(con, mini_spec(), feed)

    assert con.execute("SELECT count(*) FROM canonical_offers").fetchone()[0] == 3
    assert con.execute("SELECT DISTINCT run_id FROM offer_lineage").fetchall() == [(second.run_id,)]


def test_missing_source_column_writes_nothing(tmp_path, con):
    feed = write_feed(tmp_path / "feed.csv")
    spec = parse_spec(mini_spec().to_yaml().replace("source: vram", "source: VRAM_GB"))
    with pytest.raises(MissingSourceColumns):
        load_feed(con, spec, feed)
    assert con.execute("SELECT count(*) FROM load_runs").fetchone()[0] == 0
    temp = con.execute("SELECT count(*) FROM duckdb_tables() WHERE temporary").fetchone()[0]
    assert temp == 0
