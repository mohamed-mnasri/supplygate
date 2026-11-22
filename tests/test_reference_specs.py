"""The hand-written dev specs map generated feeds exactly to ground truth.

For every row, each canonical column must equal the ground-truth value, except the one
column a non-benign corruption touched: there the mapping must load the corrupted value
as written. Catching it is the gates' job (roadmap part 3), not the mapping's.
"""

import csv
import json
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from supplygate.mapping import load_spec, run_mapping
from supplygate.profiler import profile_feed
from supplygate.schema import canonical_columns

SPECS = Path(__file__).resolve().parents[1] / "specs"
DEV_SUPPLIERS = ["de_csv", "fr_csv", "uk_csv"]


def _norm(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (Decimal, float)):
        return f"{Decimal(str(value)):.2f}"
    if isinstance(value, str):
        try:
            return f"{Decimal(value):.2f}" if "." in value else value
        except ArithmeticError:
            return value
    return str(value)


@pytest.mark.parametrize("supplier", DEV_SUPPLIERS)
def test_reference_spec_reproduces_ground_truth(generated, supplier):
    out, _ = generated
    base = out / "dev" / supplier
    feed = next((base / "input").iterdir())
    spec = load_spec(SPECS / "dev" / f"{supplier}.yaml")

    rel = run_mapping(duckdb.connect(), spec, feed, canonical_columns())
    mapped = {r[0]: dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()}

    with open(base / "truth" / "ground_truth.csv", encoding="utf-8", newline="") as f:
        truth = {int(r["source_row"]): r for r in csv.DictReader(f)}
    corrupted = {}
    for line in (base / "truth" / "corruptions.jsonl").read_text(encoding="utf-8").splitlines():
        c = json.loads(line)
        if not c["benign"]:
            corrupted[c["source_row"]] = (c["column"], c["corrupted_value"])

    assert sorted(mapped) == sorted(truth), "row ids must line up with source_row"
    mismatches = []
    for row_id, expected in truth.items():
        got = mapped[row_id]
        assert got["_sg_errors"] == [], f"row {row_id}: {got['_sg_errors']}"
        for col in canonical_columns():
            want = expected[col]
            if row_id in corrupted and corrupted[row_id][0] == col:
                want = corrupted[row_id][1]
            if _norm(got[col]) != _norm(want):
                mismatches.append((row_id, col, got[col], want))
    assert mismatches == []


@pytest.mark.parametrize("supplier", DEV_SUPPLIERS)
def test_profile_matches_reference_source_block(generated, supplier):
    out, _ = generated
    feed = next((out / "dev" / supplier / "input").iterdir())
    spec = load_spec(SPECS / "dev" / f"{supplier}.yaml")
    opts = profile_feed(feed).source_options()
    assert (opts["format"], opts["encoding"], opts["delimiter"]) == (
        spec.source.format,
        spec.source.encoding,
        spec.source.delimiter,
    )


def test_profiler_reads_every_generated_feed(generated):
    out, manifest = generated
    for split in ("dev", "heldout"):
        for supplier_dir in (out / split).iterdir():
            for feed in (supplier_dir / "input").iterdir():
                p = profile_feed(feed)
                assert p.row_count > 0 and len(p.columns) >= 3, feed
