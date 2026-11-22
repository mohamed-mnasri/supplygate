"""Plausibility oracle: deterministic checks on mapped canonical rows.

The oracle answers one question per row: "could this offer be real?" It looks only at
canonical values (after mapping) and never at the mapping spec, so a spec cannot switch a
check off or loosen it. A row that breaks at least one rule is quarantined by the loader.

Every rule is a named SQL predicate that is TRUE when the row is implausible. Rules run
together in a single DuckDB query over the mapped table, and the names of the rules a row
breaks are collected into ``_sg_violations``. Rule names are stable identifiers: they are
stored in the quarantine table and used later to score the oracle against ground truth.

A predicate that evaluates to NULL counts as "no violation". So a range rule on
``stock_qty`` does not fire when the stock is missing; whether a value may be missing at
all is decided only by the ``required:*`` rules. This keeps each rule about one thing.

What the oracle can catch: broken identifiers (GTIN check digit), order-of-magnitude
errors (prices shifted by 100x, VRAM given in MB), values no product ships with, and
contradictions inside one row (a CPU with fewer threads than cores, a socket paired
with a memory type it does not support). What it cannot catch, by design: a value that
is wrong but plausible, such as a price off by 10% or a boost clock off by 100 MHz.
Those need the product matcher (roadmap part 4) to compare against the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb

from supplygate.mapping.compiler import quote_ident, quote_literal
from supplygate.schema import NOT_NULL, PRIMARY_KEY, Category, canonical_columns, enum_values

VIOLATIONS_COLUMN = "_sg_violations"


@dataclass(frozen=True)
class Rule:
    """One plausibility check.

    ``violation`` is a DuckDB boolean expression over canonical column names that is
    TRUE when the row breaks the rule.
    """

    name: str
    description: str
    violation: str


@dataclass(frozen=True)
class Range:
    """Inclusive numeric range."""

    low: float
    high: float

    def outside(self, column: str) -> str:
        return f"({column} < {self.low} OR {column} > {self.high})"


@dataclass(frozen=True)
class OracleConfig:
    """Domain knowledge the rules are built from.

    The defaults describe current desktop parts (AM4/AM5/LGA1700/LGA1851, DDR4/DDR5,
    GDDR6/6X/7 cards). Ranges are deliberately wide: they exist to catch values that are
    off by an order of magnitude or a unit, not to judge whether a price is a good deal.
    """

    # Offer price in EUR incl. VAT, per category. Wide enough for budget to flagship parts
    # and normal price movement; a price shifted by 100x in either direction lands outside.
    price_eur: dict[str, Range] = field(
        default_factory=lambda: {
            Category.CPU.value: Range(40, 1500),
            Category.GPU.value: Range(150, 3500),
            Category.RAM.value: Range(15, 1000),
        }
    )

    # VRAM sizes that consumer cards ship with. Odd sizes (7, 9, 11 GB) do not exist, and a
    # value given in MB (8192) is caught here too.
    gpu_vram_gb: frozenset[int] = frozenset({4, 6, 8, 10, 12, 16, 20, 24, 32, 48})

    # Which memory generations each socket's platform supports. AM4 is DDR4-only, AM5 and
    # LGA1851 are DDR5-only, LGA1700 boards exist for both.
    socket_memory: dict[str, frozenset[str]] = field(
        default_factory=lambda: {
            "AM4": frozenset({"DDR4"}),
            "AM5": frozenset({"DDR5"}),
            "LGA1700": frozenset({"DDR4", "DDR5", "DDR4/DDR5"}),
            "LGA1851": frozenset({"DDR5"}),
        }
    )

    cpu_clock_mhz: Range = Range(1000, 6500)
    cpu_tdp_w: Range = Range(15, 400)
    gpu_boost_clock_mhz: Range = Range(1000, 3500)
    gpu_tdp_w: Range = Range(30, 700)

    # RAM kits: total capacity, module count and per-module size must all be real values.
    # Checking per-module size catches "16 GB as 3 modules" and MB-for-GB slips alike.
    ram_kit_capacity_gb: frozenset[int] = frozenset({8, 16, 24, 32, 48, 64, 96, 128, 192, 256})
    ram_modules: frozenset[int] = frozenset({1, 2, 4, 8})
    ram_module_size_gb: frozenset[int] = frozenset({4, 8, 12, 16, 24, 32, 48, 64})
    ram_speed_mts: dict[str, Range] = field(default_factory=lambda: {"DDR4": Range(1600, 5333), "DDR5": Range(4000, 9600)})
    ram_cas_latency: Range = Range(9, 60)


DEFAULT_CONFIG = OracleConfig()


# --------------------------------------------------------------------------- SQL helpers


def _in_set(column: str, values: frozenset[int] | frozenset[str]) -> str:
    return f"{column} IN ({', '.join(quote_literal(v) for v in sorted(values))})"


def _is_blank(column: str, sql_type: str) -> str:
    # an empty or whitespace-only string is as missing as NULL
    if sql_type == "VARCHAR":
        return f"({column} IS NULL OR trim({column}) = '')"
    return f"{column} IS NULL"


def gtin_checksum_sql(column: str = "gtin") -> str:
    """SQL that is TRUE when a 13-digit GTIN has a correct EAN-13 check digit.

    The check digit makes the weighted digit sum (weights 1,3,1,3,... from the left,
    check digit included with weight 1) a multiple of 10. Mirrors ``gtin.is_valid``;
    a test keeps the two in agreement. TRY_CAST, because SQL does not promise to skip
    this expression for malformed values even behind an ``AND``; for those it is NULL.
    """
    terms = " + ".join(f"{3 if i % 2 else 1} * TRY_CAST(substr({column}, {i + 1}, 1) AS INTEGER)" for i in range(13))
    return f"(({terms}) % 10 = 0)"


# --------------------------------------------------------------------------- rules


def build_rules(supplier_id: str, config: OracleConfig = DEFAULT_CONFIG) -> tuple[Rule, ...]:
    """All rules for a batch from ``supplier_id``, in a fixed order.

    ``supplier_id`` is the supplier the batch is loaded for. It comes from the caller, not
    from the rows, so a mapping cannot write offers into another supplier's namespace.
    """
    columns = canonical_columns()
    rules: list[Rule] = []

    # --- presence and identity --------------------------------------------------------
    # GTIN and price are nullable in the schema (a catalog row can exist without them) but
    # an offer without either cannot be matched or sold, so the oracle requires them.
    for col in (*NOT_NULL, "gtin", "price_eur"):
        rules.append(Rule(f"required:{col}", f"{col} is missing or blank.", _is_blank(col, columns[col])))

    rules.append(
        Rule(
            "supplier_mismatch",
            f"supplier_id is not {supplier_id!r}, the supplier this batch is loaded for.",
            f"supplier_id <> {quote_literal(supplier_id)}",
        )
    )
    key = ", ".join(PRIMARY_KEY)
    rules.append(
        Rule(
            "duplicate_key",
            f"Another row earlier in the feed has the same ({key}); the first one wins.",
            f"row_number() OVER (PARTITION BY {key} ORDER BY _sg_row_id) > 1",
        )
    )

    # --- shape: values the canonical table would reject ---------------------------------
    for col, allowed in enum_values().items():
        rules.append(Rule(f"enum:{col}", f"{col} is not one of {allowed}.", f"NOT {_in_set(col, frozenset(allowed))}"))

    for cat in Category:
        own = [c for c in canonical_columns(cat) if c.startswith(f"{cat.value}_")]
        filled = " OR ".join(f"{c} IS NOT NULL" for c in own)
        rules.append(
            Rule(
                f"foreign_attributes:{cat.value}",
                f"Row has {cat.value}_* attributes but its category is not {cat.value!r}.",
                f"category IS DISTINCT FROM {quote_literal(cat.value)} AND ({filled})",
            )
        )

    # --- identifiers and commercial fields ----------------------------------------------
    rules.append(Rule("gtin_format", "GTIN is not exactly 13 digits.", "NOT regexp_full_match(gtin, '[0-9]{13}')"))
    rules.append(
        Rule(
            "gtin_checksum",
            "GTIN check digit is wrong (typo, truncation or swapped digits).",
            # only judged for well-formed GTINs; malformed ones are gtin_format's business
            f"regexp_full_match(gtin, '[0-9]{{13}}') AND NOT {gtin_checksum_sql()}",
        )
    )
    price_cases = " ".join(f"WHEN {quote_literal(cat)} THEN {rng.outside('price_eur')}" for cat, rng in sorted(config.price_eur.items()))
    rules.append(
        Rule(
            "price_range",
            "Price is outside the plausible range for its category (e.g. shifted by 100x).",
            f"CASE category {price_cases} END",
        )
    )
    rules.append(Rule("negative_stock", "Stock quantity is negative.", "stock_qty < 0"))

    # --- CPU ------------------------------------------------------------------------------
    socket_cases = " ".join(
        f"WHEN {quote_literal(socket)} THEN NOT {_in_set('cpu_memory_support', mem)}"
        for socket, mem in sorted(config.socket_memory.items())
    )
    rules.append(
        Rule(
            "cpu_socket_memory",
            "Supported memory contradicts the socket (e.g. AM5 with DDR4).",
            f"CASE cpu_socket {socket_cases} END",
        )
    )
    rules.append(
        Rule(
            "cpu_threads_vs_cores",
            "Threads must be between cores and twice the cores.",
            "cpu_threads < cpu_cores OR cpu_threads > 2 * cpu_cores",
        )
    )
    rules.append(Rule("cpu_clock_order", "Boost clock is below base clock.", "cpu_boost_clock_mhz < cpu_base_clock_mhz"))
    rules.append(
        Rule(
            "cpu_clock_range",
            "Base or boost clock is outside the plausible MHz range (GHz/MHz slip?).",
            f"{config.cpu_clock_mhz.outside('cpu_base_clock_mhz')} OR {config.cpu_clock_mhz.outside('cpu_boost_clock_mhz')}",
        )
    )
    rules.append(Rule("cpu_tdp_range", "CPU TDP is outside the plausible range.", config.cpu_tdp_w.outside("cpu_tdp_w")))

    # --- GPU ------------------------------------------------------------------------------
    rules.append(
        Rule("gpu_vram_size", "VRAM size that no card ships with (or given in MB).", f"NOT {_in_set('gpu_vram_gb', config.gpu_vram_gb)}")
    )
    rules.append(
        Rule(
            "gpu_clock_range",
            "GPU boost clock is outside the plausible MHz range.",
            config.gpu_boost_clock_mhz.outside("gpu_boost_clock_mhz"),
        )
    )
    rules.append(Rule("gpu_tdp_range", "GPU board power is outside the plausible range.", config.gpu_tdp_w.outside("gpu_tdp_w")))

    # --- RAM ------------------------------------------------------------------------------
    rules.append(
        Rule(
            "ram_kit_layout",
            "Kit capacity, module count or per-module size is not a real combination.",
            f"NOT {_in_set('ram_capacity_gb', config.ram_kit_capacity_gb)}"
            f" OR NOT {_in_set('ram_modules', config.ram_modules)}"
            f" OR ram_capacity_gb % ram_modules <> 0"
            f" OR NOT {_in_set('ram_capacity_gb // ram_modules', config.ram_module_size_gb)}",
        )
    )
    speed_cases = " ".join(
        f"WHEN {quote_literal(t)} THEN {rng.outside('ram_speed_mts')}" for t, rng in sorted(config.ram_speed_mts.items())
    )
    rules.append(
        Rule(
            "ram_speed_range",
            "Data rate is outside the range of its memory generation (MHz vs MT/s slip?).",
            f"CASE ram_type {speed_cases} END",
        )
    )
    rules.append(
        Rule("ram_cas_latency_range", "CAS latency is outside the plausible range.", config.ram_cas_latency.outside("ram_cas_latency"))
    )

    names = [r.name for r in rules]
    assert len(names) == len(set(names)), "rule names must be unique"
    return tuple(rules)


# --------------------------------------------------------------------------- running


def oracle_sql(table: str, rules: tuple[Rule, ...]) -> str:
    """SELECT every column of ``table`` plus ``_sg_violations`` (sorted rule names)."""
    if not rules:
        return f"SELECT *, CAST([] AS VARCHAR[]) AS {VIOLATIONS_COLUMN} FROM {quote_ident(table)}"
    # COALESCE turns "unknown" (a NULL input) into "no violation"; see the module docstring
    cases = ", ".join(f"CASE WHEN COALESCE({r.violation}, FALSE) THEN {quote_literal(r.name)} END" for r in rules)
    # list_distinct drops the NULLs left by rules that did not fire
    return (
        f"SELECT *, CAST(list_sort(list_distinct([{cases}])) AS VARCHAR[]) AS {VIOLATIONS_COLUMN} "
        f"FROM {quote_ident(table)} ORDER BY _sg_row_id"
    )


def run_oracle(
    con: duckdb.DuckDBPyConnection, table: str, supplier_id: str, config: OracleConfig = DEFAULT_CONFIG
) -> duckdb.DuckDBPyRelation:
    """Check the mapped rows in ``table`` (needs ``_sg_row_id`` and canonical columns)."""
    return con.sql(oracle_sql(table, build_rules(supplier_id, config)))
