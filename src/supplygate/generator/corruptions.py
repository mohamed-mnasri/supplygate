"""Corruption families applied to synthetic supplier rows, with a log entry per change.

Format differences between suppliers (headers, encodings, units, decimal commas)
are NOT corruptions: they are the mapping problem. Corruptions are row/cell errors
that a correct mapping still has to catch (quarantine) or, for ``benign`` ones,
tolerate (normalize and load).

Each family belongs to a split:

* ``dev``     - used while developing the oracle and mapping;
* ``heldout`` - only appears in held-out supplier feeds and is scored separately,
  so the oracle cannot be tuned to it.

``expected_oracle_visible`` records whether a correct plausibility oracle *should*
be able to flag the change. Families with ``False`` exist to measure oracle limits
(plausible-but-wrong values pass every gate by design).

To add a family: write an ``_apply`` function that changes one value of a
RowState and returns a Change describing it, then add a Family entry to
FAMILIES. The generator and the tests pick it up from there.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from supplygate.gtin import is_valid
from supplygate.schema import Category

# VRAM sizes that consumer and workstation cards actually ship with.
VALID_VRAM_GB = (4, 6, 8, 10, 12, 16, 20, 24, 32, 48)
_INVALID_VRAM_GB = (5, 7, 9, 11, 13, 15, 18)
# A memory type each socket does NOT support (AM5 boards are DDR5 only, etc.).
_SOCKET_WRONG_MEMORY = {"AM4": "DDR5", "AM5": "DDR4", "LGA1851": "DDR4"}


@dataclass
class RowState:
    """A row being generated: canonical values plus the price in the supplier's currency.

    ``price_to_eur`` converts the native price into canonical EUR the same way a
    correct mapping would, so price corruptions stay consistent across both.
    """

    values: dict[str, Any]
    native_price: Decimal | None
    price_to_eur: Callable[[Decimal], Decimal]
    product_id: str

    def set_native_price(self, native: Decimal | None) -> None:
        self.native_price = native
        self.values["price_eur"] = None if native is None else self.price_to_eur(native)


@dataclass(frozen=True)
class Change:
    """What a corruption did to one cell. Written to corruptions.jsonl."""

    column: str
    clean_value: Any
    corrupted_value: Any
    expected_oracle_visible: bool


@dataclass(frozen=True)
class Family:
    """A kind of corruption, e.g. "wrong GTIN check digit".

    ``apply`` changes the row and returns a Change, or None if it could not
    apply after all. ``applicable`` is checked first so ``apply`` can assume
    the values it needs are present.
    """

    name: str
    split: str  # "dev" | "heldout"
    description: str
    categories: frozenset[Category]
    apply: Callable[[RowState, random.Random], Change | None]
    benign: bool = False
    duplicates_row: bool = False
    applicable: Callable[[RowState], bool] = field(default=lambda row: True)


ALL = frozenset(Category)


def _set(row: RowState, column: str, value: Any, visible: bool) -> Change:
    """Overwrite one canonical value and record the before/after."""
    old = row.values.get(column)
    row.values[column] = value
    return Change(column, old, value, visible)


# --- dev families -------------------------------------------------------------


def _gtin_bad_checksum(row: RowState, rng: random.Random) -> Change:
    g = row.values["gtin"]
    wrong = rng.choice([d for d in "0123456789" if d != g[12]])
    return _set(row, "gtin", g[:12] + wrong, True)


def _missing_required(row: RowState, rng: random.Random) -> Change:
    column = rng.choice(["gtin", "price_eur"])
    if column == "price_eur":
        old = row.values["price_eur"]
        row.set_native_price(None)
        return Change("price_eur", old, None, True)
    return _set(row, "gtin", None, True)


def _price_decimal_shift(row: RowState, rng: random.Random) -> Change:
    old = row.values["price_eur"]
    # A misplaced decimal point: 399.90 becomes 39990.00 or 4.00.
    factor = rng.choice([Decimal(100), Decimal("0.01")])
    assert row.native_price is not None
    row.set_native_price((row.native_price * factor).quantize(row.native_price))
    return Change("price_eur", old, row.values["price_eur"], True)


def _vram_invalid(row: RowState, rng: random.Random) -> Change:
    return _set(row, "gpu_vram_gb", rng.choice(_INVALID_VRAM_GB), True)


def _negative_stock(row: RowState, rng: random.Random) -> Change:
    return _set(row, "stock_qty", -rng.randint(1, 50), True)


def _duplicate_row(row: RowState, rng: random.Random) -> Change:
    # Nothing to change in the values: the generator inserts the copy itself
    # (Family.duplicates_row). This only produces the log entry for the copy.
    return Change("supplier_sku", row.values["supplier_sku"], row.values["supplier_sku"], True)


def _cosmetic_noise(row: RowState, rng: random.Random) -> Change:
    column = rng.choice(["brand", "model_name"])
    value: str = row.values[column]
    noisy = rng.choice([f"  {value}", f"{value}   ", f" {value} ", value.replace(" ", "  ", 1)])
    if noisy == value:
        noisy = f" {value} "
    return _set(row, column, noisy, False)


def _price_drift(row: RowState, rng: random.Random) -> Change:
    old = row.values["price_eur"]
    assert row.native_price is not None
    factor = Decimal(str(round(rng.choice([rng.uniform(0.80, 0.92), rng.uniform(1.08, 1.20)]), 3)))
    row.set_native_price((row.native_price * factor).quantize(row.native_price))
    return Change("price_eur", old, row.values["price_eur"], False)


# --- held-out families ----------------------------------------------------------


def _gtin_transposition(row: RowState, rng: random.Random) -> Change | None:
    g = row.values["gtin"]
    positions = [i for i in range(11) if g[i] != g[i + 1]]
    if not positions:
        return None
    i = rng.choice(positions)
    swapped = g[:i] + g[i + 1] + g[i] + g[i + 2 :]
    # EAN-13 misses a transposition of adjacent digits that differ by 5.
    return _set(row, "gtin", swapped, not is_valid(swapped))


def _socket_memory_mismatch(row: RowState, rng: random.Random) -> Change:
    return _set(row, "cpu_memory_support", _SOCKET_WRONG_MEMORY[row.values["cpu_socket"]], True)


def _unit_slip_mb(row: RowState, rng: random.Random) -> Change:
    column = "gpu_vram_gb" if row.values["category"] == Category.GPU else "ram_capacity_gb"
    return _set(row, column, row.values[column] * 1024, True)


def _spec_plausible_wrong(row: RowState, rng: random.Random) -> Change:
    cat = row.values["category"]
    if cat == Category.CPU:
        return _set(row, "cpu_boost_clock_mhz", row.values["cpu_boost_clock_mhz"] + rng.choice([-200, -100, 100, 200]), False)
    if cat == Category.GPU:
        return _set(row, "gpu_tdp_w", row.values["gpu_tdp_w"] + rng.choice([-30, -15, 15, 30]), False)
    cl = row.values["ram_cas_latency"]
    return _set(row, "ram_cas_latency", cl + rng.choice([-2, 2, 4]), False)


FAMILIES: tuple[Family, ...] = (
    Family("gtin_bad_checksum", "dev", "Wrong GTIN check digit.", ALL, _gtin_bad_checksum,
           applicable=lambda r: r.values.get("gtin") is not None),
    Family("missing_required", "dev", "GTIN or price left empty.", ALL, _missing_required,
           applicable=lambda r: r.values.get("gtin") is not None and r.native_price is not None),
    Family("price_decimal_shift", "dev", "Price multiplied or divided by 100.", ALL, _price_decimal_shift,
           applicable=lambda r: r.native_price is not None),
    Family("vram_invalid", "dev", "VRAM size that no card ships with.", frozenset({Category.GPU}), _vram_invalid),
    Family("negative_stock", "dev", "Negative stock quantity.", ALL, _negative_stock),
    Family("duplicate_row", "dev", "Same supplier SKU appears twice.", ALL, _duplicate_row, duplicates_row=True),
    Family("cosmetic_noise", "dev", "Extra whitespace in brand or model name; should load after trimming.", ALL,
           _cosmetic_noise, benign=True),
    Family("price_drift", "dev", "Price off by 8-20%, still plausible. Oracle limit.", ALL, _price_drift,
           applicable=lambda r: r.native_price is not None),
    Family("gtin_transposition", "heldout", "Two adjacent GTIN digits swapped.", ALL, _gtin_transposition,
           applicable=lambda r: r.values.get("gtin") is not None),
    Family("socket_memory_mismatch", "heldout", "CPU memory support contradicts its socket.",
           frozenset({Category.CPU}), _socket_memory_mismatch,
           applicable=lambda r: r.values.get("cpu_socket") in _SOCKET_WRONG_MEMORY),
    Family("unit_slip_mb", "heldout", "VRAM or RAM capacity given in MB instead of GB.",
           frozenset({Category.GPU, Category.RAM}), _unit_slip_mb),
    Family("spec_plausible_wrong", "heldout", "Spec value changed to another plausible value. Oracle limit.", ALL,
           _spec_plausible_wrong),
)

FAMILIES_BY_NAME = {f.name: f for f in FAMILIES}


def families_for_split(split: str) -> tuple[Family, ...]:
    """Dev feeds get dev families; held-out feeds get dev and held-out families."""
    if split == "dev":
        return tuple(f for f in FAMILIES if f.split == "dev")
    if split == "heldout":
        return FAMILIES
    raise ValueError(f"unknown split {split!r}")
