from decimal import Decimal

import duckdb
import pytest
from pydantic import ValidationError

from supplygate import schema
from supplygate.schema import CanonicalOffer


def _gpu(**kw):
    base = dict(supplier_id="s", supplier_sku="1", category="gpu", brand="NVIDIA", model_name="GeForce RTX 4070")
    return CanonicalOffer(**(base | kw))


def test_columns_are_ordered_and_typed():
    cols = schema.canonical_columns()
    assert list(cols)[:3] == ["supplier_id", "supplier_sku", "category"]
    assert cols["price_eur"] == "DECIMAL(10,2)"
    assert cols["gpu_vram_gb"] == "INTEGER"
    assert list(cols) == list(CanonicalOffer.model_fields)


def test_category_filter_keeps_common_and_own_columns():
    gpu = schema.canonical_columns("gpu")
    assert "gpu_vram_gb" in gpu and "price_eur" in gpu
    assert not any(c.startswith(("cpu_", "ram_")) for c in gpu)
    assert set(schema.canonical_columns("cpu")) & set(schema.canonical_columns("ram")) == {
        c for c in schema.canonical_columns() if not c.startswith(("cpu_", "gpu_", "ram_"))
    }


def test_units_match_column_suffixes():
    for col, unit in schema.column_units().items():
        suffix = {"EUR": "_eur", "MHz": "_mhz", "W": "_w", "GB": "_gb", "MT/s": "_mts"}[unit]
        assert col.endswith(suffix)


def test_rejects_other_category_attributes():
    with pytest.raises(ValidationError, match="cpu_cores must be NULL"):
        _gpu(cpu_cores=8)


def test_rejects_bad_enum_and_gtin_shape_and_extra_fields():
    with pytest.raises(ValidationError):
        _gpu(gpu_vram_type="GDDR5")
    with pytest.raises(ValidationError):
        _gpu(gtin="12345")
    with pytest.raises(ValidationError):
        _gpu(vram=8)


def test_schema_does_not_check_gtin_checksum():
    # Checksums belong to the oracle, which must stay independent of the schema.
    assert _gpu(gtin="4006381333932").gtin == "4006381333932"


def test_price_is_two_decimals():
    with pytest.raises(ValidationError):
        _gpu(price_eur=Decimal("1.234"))


def test_ddl_roundtrip_in_duckdb():
    con = duckdb.connect()
    schema.create_tables(con)
    offer = _gpu(gtin="2000000000008", price_eur=Decimal("549.90"), gpu_vram_gb=12, gpu_vram_type="GDDR6X")
    schema.insert_offers(con, [offer])
    row = con.execute("SELECT price_eur, gpu_vram_type, cpu_cores FROM canonical_offers").fetchone()
    assert row == (Decimal("549.90"), "GDDR6X", None)
    described = {r[0]: r[1] for r in con.execute("DESCRIBE canonical_offers").fetchall()}
    assert described["price_eur"] == "DECIMAL(10,2)"
    assert list(described) == list(schema.canonical_columns())


def test_ddl_enforces_primary_key_and_enum_checks():
    con = duckdb.connect()
    schema.create_tables(con)
    schema.insert_offers(con, [_gpu()])
    with pytest.raises(duckdb.ConstraintException):
        schema.insert_offers(con, [_gpu()])
    with pytest.raises(duckdb.ConstraintException):
        con.execute("INSERT INTO canonical_offers (supplier_id, supplier_sku, category, brand, model_name) "
                    "VALUES ('s', '2', 'psu', 'x', 'y')")  # fmt: skip
