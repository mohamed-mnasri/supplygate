"""Canonical catalog schema.

One wide table, ``canonical_offers``: one row per supplier offer, with common
columns plus category-specific columns (prefixed ``cpu_``, ``gpu_``, ``ram_``).
Columns of the other categories must be NULL. Units are fixed and appear in the
column name (``_mhz``, ``_gb``, ``_w``, ``_eur``, ``_mts``).

The Pydantic model :class:`CanonicalOffer` is the single source of truth: the
DuckDB DDL and :func:`canonical_columns` are derived from its field metadata.

What this schema checks: types, enums, value shape (e.g. GTIN is 13 digits) and
that a row only carries attributes of its own category. What it does NOT check:
GTIN checksums, plausible values, cross-field compatibility. Those belong to the
plausibility oracle (roadmap item 3), which must stay independent of mapping.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

import duckdb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Category(StrEnum):
    CPU = "cpu"
    GPU = "gpu"
    RAM = "ram"


class CpuSocket(StrEnum):
    AM4 = "AM4"
    AM5 = "AM5"
    LGA1700 = "LGA1700"
    LGA1851 = "LGA1851"


class MemoryType(StrEnum):
    DDR4 = "DDR4"
    DDR5 = "DDR5"


class CpuMemorySupport(StrEnum):
    DDR4 = "DDR4"
    DDR5 = "DDR5"
    DDR4_DDR5 = "DDR4/DDR5"


class VramType(StrEnum):
    GDDR6 = "GDDR6"
    GDDR6X = "GDDR6X"
    GDDR7 = "GDDR7"


def _col(duckdb_type: str, description: str, *, unit: str | None = None, category: Category | None = None, **kw: Any) -> Any:
    """Field with the metadata used to derive DDL and column listings."""
    extra: dict[str, Any] = {"duckdb_type": duckdb_type, "unit": unit, "category": category.value if category else None}
    return Field(description=description, json_schema_extra=extra, **kw)


_CPU, _GPU, _RAM = Category.CPU, Category.GPU, Category.RAM


class CanonicalOffer(BaseModel):
    """One supplier offer in canonical form. Field order is column order."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    # --- identity / common -------------------------------------------------
    supplier_id: str = _col("VARCHAR", "Supplier identifier, e.g. 'de_csv'.", min_length=1)
    supplier_sku: str = _col("VARCHAR", "Supplier's own article number.", min_length=1)
    category: Category = _col("VARCHAR", "Product category: cpu, gpu or ram.")
    gtin: str | None = _col("VARCHAR", "GTIN-13 / EAN-13 as 13 digits. Checksum is checked by the oracle, not here.", default=None)
    mpn: str | None = _col("VARCHAR", "Manufacturer part number.", default=None)
    brand: str = _col("VARCHAR", "Manufacturer / brand name, e.g. 'AMD'.", min_length=1)
    model_name: str = _col("VARCHAR", "Product model name without brand, e.g. 'Ryzen 7 7800X3D'.", min_length=1)
    price_eur: Decimal | None = _col(
        "DECIMAL(10,2)", "Offer price in EUR incl. VAT, 2 decimals.", unit="EUR", default=None, max_digits=10, decimal_places=2
    )
    stock_qty: int | None = _col("INTEGER", "Units available at the supplier.", default=None)

    # --- CPU ---------------------------------------------------------------
    cpu_socket: CpuSocket | None = _col("VARCHAR", "CPU socket.", category=_CPU, default=None)
    cpu_cores: int | None = _col("INTEGER", "Physical cores (P+E).", category=_CPU, default=None)
    cpu_threads: int | None = _col("INTEGER", "Hardware threads.", category=_CPU, default=None)
    cpu_base_clock_mhz: int | None = _col("INTEGER", "Base clock (P-core base for hybrid CPUs).", unit="MHz", category=_CPU, default=None)
    cpu_boost_clock_mhz: int | None = _col("INTEGER", "Max boost clock.", unit="MHz", category=_CPU, default=None)
    cpu_tdp_w: int | None = _col("INTEGER", "TDP / base power.", unit="W", category=_CPU, default=None)
    cpu_memory_support: CpuMemorySupport | None = _col("VARCHAR", "Supported memory generation(s).", category=_CPU, default=None)

    # --- GPU ---------------------------------------------------------------
    gpu_chipset: str | None = _col("VARCHAR", "GPU chip, e.g. 'GeForce RTX 4070'.", category=_GPU, default=None)
    gpu_vram_gb: int | None = _col("INTEGER", "Video memory.", unit="GB", category=_GPU, default=None)
    gpu_vram_type: VramType | None = _col("VARCHAR", "Video memory type.", category=_GPU, default=None)
    gpu_boost_clock_mhz: int | None = _col("INTEGER", "Boost clock.", unit="MHz", category=_GPU, default=None)
    gpu_tdp_w: int | None = _col("INTEGER", "Total board power.", unit="W", category=_GPU, default=None)

    # --- RAM ---------------------------------------------------------------
    ram_type: MemoryType | None = _col("VARCHAR", "Memory generation.", category=_RAM, default=None)
    ram_capacity_gb: int | None = _col("INTEGER", "Total kit capacity.", unit="GB", category=_RAM, default=None)
    ram_modules: int | None = _col("INTEGER", "Number of modules in the kit.", category=_RAM, default=None)
    ram_speed_mts: int | None = _col("INTEGER", "Data rate.", unit="MT/s", category=_RAM, default=None)
    ram_cas_latency: int | None = _col("INTEGER", "CAS latency (CL).", category=_RAM, default=None)

    @field_validator("gtin")
    @classmethod
    def _gtin_shape(cls, v: str | None) -> str | None:
        if v is not None and not (len(v) == 13 and v.isdigit()):
            raise ValueError("gtin must be exactly 13 digits")
        return v

    @model_validator(mode="after")
    def _only_own_category_attributes(self) -> CanonicalOffer:
        own = Category(self.category).value
        for name, cat in _FIELD_CATEGORY.items():
            if cat is not None and cat != own and getattr(self, name) is not None:
                raise ValueError(f"{name} must be NULL for category {own!r}")
        return self


def _meta(name: str) -> dict[str, Any]:
    extra = CanonicalOffer.model_fields[name].json_schema_extra
    assert isinstance(extra, dict)
    return extra


_FIELD_CATEGORY: dict[str, str | None] = {n: _meta(n)["category"] for n in CanonicalOffer.model_fields}

TABLE_NAME = "canonical_offers"
PRIMARY_KEY = ("supplier_id", "supplier_sku")
NOT_NULL = ("supplier_id", "supplier_sku", "category", "brand", "model_name")


def canonical_columns(category: str | Category | None = None) -> dict[str, str]:
    """Ordered mapping of canonical column name -> DuckDB type.

    With ``category``, returns the common columns plus that category's columns.
    """
    cat = Category(category).value if category is not None else None
    return {
        name: _meta(name)["duckdb_type"]
        for name in CanonicalOffer.model_fields
        if cat is None or _FIELD_CATEGORY[name] in (None, cat)
    }


def column_units() -> dict[str, str]:
    """Canonical column name -> unit, for columns that have one."""
    return {n: _meta(n)["unit"] for n in CanonicalOffer.model_fields if _meta(n)["unit"]}


def column_descriptions() -> dict[str, str]:
    return {n: f.description or "" for n, f in CanonicalOffer.model_fields.items()}


def enum_values() -> dict[str, list[str]]:
    """Allowed values for enum-typed columns."""
    return {
        "category": [e.value for e in Category],
        "cpu_socket": [e.value for e in CpuSocket],
        "cpu_memory_support": [e.value for e in CpuMemorySupport],
        "gpu_vram_type": [e.value for e in VramType],
        "ram_type": [e.value for e in MemoryType],
    }


def ddl(table: str = TABLE_NAME) -> str:
    """CREATE TABLE statement for the canonical table, derived from the model."""
    lines = []
    enums = enum_values()
    for name, typ in canonical_columns().items():
        line = f"    {name} {typ}"
        if name in NOT_NULL:
            line += " NOT NULL"
        if name in enums:
            allowed = ", ".join(f"'{v}'" for v in enums[name])
            line += f" CHECK ({name} IN ({allowed}))"
        lines.append(line)
    lines.append(f"    PRIMARY KEY ({', '.join(PRIMARY_KEY)})")
    return f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(lines) + "\n);"


def create_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(ddl())


def insert_offers(con: duckdb.DuckDBPyConnection, offers: list[CanonicalOffer], table: str = TABLE_NAME) -> None:
    cols = list(canonical_columns())
    placeholders = ", ".join("?" for _ in cols)
    rows = [[getattr(o, c) for c in cols] for o in offers]
    if rows:
        con.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", rows)
