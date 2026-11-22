import csv
import io
import json
from decimal import Decimal
from pathlib import Path

import pytest
from feed_decoders import DECODERS, decode_spec_txt

from supplygate.catalog import load_catalog
from supplygate.cli import main
from supplygate.generator import SUPPLIERS, GenerationConfig, generate
from supplygate.generator.corruptions import FAMILIES_BY_NAME
from supplygate.schema import CanonicalOffer, canonical_columns

INT_COLS = {c for c, t in canonical_columns().items() if t == "INTEGER"}


def _truth(out: Path, info: dict) -> list[dict]:
    text = (out / info["path"] / "truth/ground_truth.csv").read_text(encoding="utf-8")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        for k, v in r.items():
            if v == "":
                r[k] = None
            elif k in INT_COLS or k == "source_row":
                r[k] = int(v)
            elif k == "price_eur":
                r[k] = Decimal(v)
        rows.append(r)
    return rows


def _corruptions(out: Path, info: dict) -> list[dict]:
    lines = (out / info["path"] / "truth/corruptions.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _decoded(out: Path, sid: str, info: dict) -> list[dict]:
    path = out / info["path"] / "input"
    if sid == "spec_txt":
        return decode_spec_txt(path, {})
    return DECODERS[sid](path)


def _as_truth_type(col: str, v):
    if isinstance(v, Decimal) or v is None or col in INT_COLS:
        return v
    return str(v)


def test_all_six_suppliers_with_split(generated):
    _, manifest = generated
    splits = {sid: info["split"] for sid, info in manifest["suppliers"].items()}
    assert splits == {"de_csv": "dev", "fr_csv": "dev", "uk_csv": "dev",
                      "spec_txt": "heldout", "legacy_erp": "heldout", "json_api": "heldout"}  # fmt: skip


def test_generation_is_deterministic(tmp_path, generated):
    out, manifest = generated
    again = generate(tmp_path, GenerationConfig(**manifest["config"] | {"suppliers": tuple(manifest["config"]["suppliers"])}))
    assert again == manifest
    for f in out.rglob("*"):
        if f.is_file():
            assert (tmp_path / f.relative_to(out)).read_bytes() == f.read_bytes(), f


def test_different_seed_gives_different_feeds(tmp_path, generated):
    _, manifest = generated
    other = generate(tmp_path, GenerationConfig(seed=manifest["config"]["seed"] + 1))
    assert other["suppliers"]["de_csv"]["input_files"] != manifest["suppliers"]["de_csv"]["input_files"]


def test_truth_is_separate_from_input(generated):
    out, manifest = generated
    for info in manifest["suppliers"].values():
        inputs = {p.name for p in (out / info["path"] / "input").iterdir()}
        assert inputs == {k.removeprefix("input/") for k in info["input_files"]}
        assert not any("truth" in n or "corruption" in n for n in inputs)


def test_ground_truth_rows_are_valid_canonical_offers(generated):
    out, manifest = generated
    catalog = {p.product_id for p in load_catalog()}
    for info in manifest["suppliers"].values():
        rows = _truth(out, info)
        assert len(rows) == info["rows"]
        assert [r["source_row"] for r in rows] == list(range(1, len(rows) + 1))
        for r in rows:
            assert r["product_id"] in catalog
            CanonicalOffer(**{c: r[c] for c in canonical_columns()})


def test_input_files_match_manifest_encoding(generated):
    out, manifest = generated
    for info in manifest["suppliers"].values():
        for rel, meta in info["input_files"].items():
            (out / info["path"] / rel).read_bytes().decode(meta["encoding"])
    fr = (out / "dev/fr_csv/input/feed.csv").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        fr.decode("utf-8")
    assert "Désignation" in fr.decode("latin-1")


@pytest.mark.parametrize("sid", list(SUPPLIERS))
def test_clean_rows_decode_exactly_to_ground_truth(generated, sid):
    """Every column a supplier `provides` is recoverable from its feed for uncorrupted rows."""
    out, manifest = generated
    info = manifest["suppliers"][sid]
    truth = _truth(out, info)
    decoded = _decoded(out, sid, info)
    assert len(decoded) == len(truth)
    checked = 0
    for t, d in zip(truth, decoded, strict=True):
        if t["corruption"]:
            continue
        cols = [c for c in info["provides"] if c in d] if sid == "spec_txt" else info["provides"]
        for col in cols:
            if col == "supplier_id":
                continue
            assert _as_truth_type(col, d.get(col)) == t[col], (sid, t["source_row"], col)
        if sid == "spec_txt":
            assert d["_head"] == f"{t['brand']} {t['model_name']}"
        checked += 1
    assert checked >= 5


@pytest.mark.parametrize("sid", list(SUPPLIERS))
def test_corruption_log_matches_feed(generated, sid):
    out, manifest = generated
    info = manifest["suppliers"][sid]
    truth = {r["source_row"]: r for r in _truth(out, info)}
    decoded = _decoded(out, sid, info)
    records = _corruptions(out, info)
    assert len(records) == info["corrupted_rows"]
    for rec in records:
        t = truth[rec["source_row"]]
        assert t["corruption"] == rec["family"]
        assert t["expected_outcome"] == rec["expected_outcome"]
        assert t["supplier_sku"] == rec["supplier_sku"]
        col = rec["column"]
        if col not in info["provides"] or col == "supplier_sku" or FAMILIES_BY_NAME[rec["family"]].benign:
            continue
        got = _as_truth_type(col, decoded[rec["source_row"] - 1].get(col))
        expected = rec["corrupted_value"]
        expected = Decimal(expected) if col == "price_eur" and expected is not None else expected
        assert got == expected, (sid, rec)
    uncorrupted = {r for r in truth} - {rec["source_row"] for rec in records}
    assert all(truth[r]["corruption"] is None for r in uncorrupted)


def test_heldout_families_only_in_heldout_feeds(generated):
    out, manifest = generated
    seen_heldout = 0
    for info in manifest["suppliers"].values():
        for rec in _corruptions(out, info):
            assert FAMILIES_BY_NAME[rec["family"]].split == rec["family_split"]
            if rec["family_split"] == "heldout":
                assert info["split"] == "heldout"
                seen_heldout += 1
    assert seen_heldout > 0


def test_duplicate_rows_share_sku_and_only_copy_is_quarantined(generated):
    out, manifest = generated
    found = 0
    for info in manifest["suppliers"].values():
        truth = _truth(out, info)
        for rec in _corruptions(out, info):
            if rec["family"] != "duplicate_row":
                continue
            found += 1
            original, copy = truth[rec["source_row"] - 2], truth[rec["source_row"] - 1]
            assert original["supplier_sku"] == copy["supplier_sku"]
            assert original["expected_outcome"] == "load" and copy["expected_outcome"] == "quarantine"
    assert found > 0


def test_every_family_appears_at_high_corruption_rate(tmp_path):
    manifest = generate(tmp_path, GenerationConfig(seed=1, corruption_rate=1.0))
    seen = set()
    for info in manifest["suppliers"].values():
        seen |= set(info["corruptions_by_family"])
    assert seen == set(FAMILIES_BY_NAME)


def test_zero_corruption_rate_gives_clean_feeds(tmp_path):
    manifest = generate(tmp_path, GenerationConfig(seed=1, corruption_rate=0.0))
    assert all(info["corrupted_rows"] == 0 for info in manifest["suppliers"].values())


def test_cli_generate_and_init_db(tmp_path, capsys):
    assert main(["generate", "--out", str(tmp_path / "gen"), "--supplier", "de_csv"]) == 0
    assert (tmp_path / "gen/manifest.json").exists()
    assert (tmp_path / "gen/dev/de_csv/input/feed.csv").exists()
    assert main(["init-db", "--db", str(tmp_path / "x.duckdb")]) == 0
    assert "de_csv" in capsys.readouterr().out
