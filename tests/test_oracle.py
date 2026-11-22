"""Plausibility oracle: each rule fires on the defect it targets and on nothing else."""

import random
from decimal import Decimal

import duckdb
import pytest

from supplygate.catalog import load_catalog
from supplygate.gates.oracle import VIOLATIONS_COLUMN, build_rules, gtin_checksum_sql, oracle_sql
from supplygate.gtin import is_valid
from supplygate.schema import canonical_columns

SUPPLIER = "test_sup"


def clean_offer(category: str, n: int = 0) -> dict:
    """A correct canonical offer for the n-th catalog product of ``category``."""
    product = [p for p in load_catalog() if p.category == category][n]
    price = product.reference_price_eur.quantize(Decimal("0.01"))
    return product.to_offer(SUPPLIER, f"SKU-{product.product_id}", price, 5).model_dump()


def violations(rows: list[dict]) -> list[list[str]]:
    """Load ``rows`` as a mapped batch and return the violations per row."""
    con = duckdb.connect()
    cols = canonical_columns()
    con.execute("CREATE TABLE mapped (_sg_row_id INTEGER, " + ", ".join(f"{c} {t}" for c, t in cols.items()) + ")")
    placeholders = ", ".join("?" for _ in range(len(cols) + 1))
    con.executemany(
        f"INSERT INTO mapped VALUES ({placeholders})",
        [[i, *(row.get(c) for c in cols)] for i, row in enumerate(rows, start=1)],
    )
    rel = con.sql(oracle_sql("mapped", build_rules(SUPPLIER)))
    idx = rel.columns.index(VIOLATIONS_COLUMN)
    return [r[idx] for r in rel.fetchall()]


def test_every_catalog_product_passes_as_a_clean_offer():
    # Guards the config: a rule that rejects real products would quarantine good data.
    rows = [
        p.to_offer(SUPPLIER, f"SKU-{i}", p.reference_price_eur.quantize(Decimal("0.01")), 3).model_dump()
        for i, p in enumerate(load_catalog())
    ]
    assert all(v == [] for v in violations(rows))


@pytest.mark.parametrize(
    ("category", "change", "rule"),
    [
        # presence and identity
        ("cpu", {"gtin": None}, "required:gtin"),
        ("cpu", {"price_eur": None}, "required:price_eur"),
        ("gpu", {"brand": "   "}, "required:brand"),
        ("ram", {"supplier_id": "someone_else"}, "supplier_mismatch"),
        # shape
        ("cpu", {"cpu_socket": "AM3"}, "enum:cpu_socket"),
        ("gpu", {"gpu_vram_type": "HBM3"}, "enum:gpu_vram_type"),
        # identifiers and commercial fields
        ("cpu", {"gtin": "12345"}, "gtin_format"),
        ("ram", {"price_eur": Decimal("0.39")}, "price_range"),
        ("cpu", {"price_eur": Decimal("12900.00")}, "price_range"),
        ("gpu", {"stock_qty": -3}, "negative_stock"),
        # CPU
        ("cpu", {"cpu_socket": "AM5", "cpu_memory_support": "DDR4"}, "cpu_socket_memory"),
        ("cpu", {"cpu_threads": 4}, "cpu_threads_vs_cores"),
        ("cpu", {"cpu_boost_clock_mhz": 3000}, "cpu_clock_order"),
        ("cpu", {"cpu_base_clock_mhz": 4, "cpu_boost_clock_mhz": 5}, "cpu_clock_range"),
        ("cpu", {"cpu_tdp_w": 1200}, "cpu_tdp_range"),
        # GPU
        ("gpu", {"gpu_vram_gb": 7}, "gpu_vram_size"),
        ("gpu", {"gpu_vram_gb": 8192}, "gpu_vram_size"),
        ("gpu", {"gpu_boost_clock_mhz": 25000}, "gpu_clock_range"),
        ("gpu", {"gpu_tdp_w": 5}, "gpu_tdp_range"),
        # RAM
        ("ram", {"ram_capacity_gb": 16384}, "ram_kit_layout"),
        ("ram", {"ram_modules": 3}, "ram_kit_layout"),
        ("ram", {"ram_type": "DDR4", "ram_speed_mts": 800}, "ram_speed_range"),
        ("ram", {"ram_cas_latency": 160}, "ram_cas_latency_range"),
    ],
)
def test_rule_fires_only_on_its_defect(category, change, rule):
    row = clean_offer(category) | change
    assert violations([row]) == [[rule]]


def test_wrong_check_digit_is_caught():
    row = clean_offer("gpu")
    g = row["gtin"]
    row["gtin"] = g[:12] + str((int(g[12]) + 1) % 10)
    assert violations([row]) == [["gtin_checksum"]]


def test_attributes_of_another_category_are_caught():
    row = clean_offer("ram") | {"gpu_vram_gb": 8}
    assert violations([row]) == [["foreign_attributes:gpu"]]


def test_later_duplicate_key_is_flagged_first_one_is_kept():
    row = clean_offer("cpu")
    other = clean_offer("cpu", 1)
    assert violations([row, other, dict(row)]) == [[], [], ["duplicate_key"]]


def test_unknown_values_do_not_fire_range_rules():
    # A missing optional value is not a violation; required:* decides what may be missing.
    row = clean_offer("cpu") | {"stock_qty": None, "cpu_threads": None, "cpu_tdp_w": None}
    assert violations([row]) == [[]]


def test_several_defects_are_all_reported_sorted():
    row = clean_offer("gpu") | {"gpu_vram_gb": 9, "stock_qty": -1}
    assert violations([row]) == [["gpu_vram_size", "negative_stock"]]


def test_sql_checksum_agrees_with_python_checksum():
    rng = random.Random(0)
    samples = ["".join(rng.choice("0123456789") for _ in range(13)) for _ in range(2000)]
    con = duckdb.connect()
    con.execute("CREATE TABLE g (gtin VARCHAR)")
    con.executemany("INSERT INTO g VALUES (?)", [[s] for s in samples])
    got = dict(con.execute(f"SELECT gtin, {gtin_checksum_sql()} FROM g").fetchall())
    assert got == {s: is_valid(s) for s in samples}


def test_rule_names_are_unique_and_described():
    rules = build_rules(SUPPLIER)
    assert len({r.name for r in rules}) == len(rules)
    assert all(r.description for r in rules)
