"""Mapping spec: the declarative contract between a supplier feed and the canonical catalog.

A spec is a small YAML document. Every operation in it comes from a closed set, every
value is a literal, and nothing in it is SQL. The compiler (``compiler.py``) turns it into
DuckDB SQL, so an LLM that proposes a spec can only choose among these operations.

Example::

    version: 1
    supplier: de_csv
    source:
      format: csv
      encoding: utf-8
      delimiter: ";"
    columns:
      - target: price_eur
        source: Preis
        transforms:
          - parse_number: {decimal: ",", thousands: "."}
      - target: memory_gb
        source: Speicher
        transforms:
          - regex_extract: '(\\d+)\\s*GB'
          - cast: INTEGER

Four operations are flagged as *risky* because they can make bad data look clean:
``filter_rows`` (drops rows), ``default_value`` (fills missing values), ``null_if`` (turns
values into NULL) and ``cast_loose`` (swallows cast failures). They are allowed, but
``MappingSpec.risky_ops()`` lists every use so the router and the gates can treat them
with suspicion.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SPEC_VERSION = 1

RISKY_OPS: frozenset[str] = frozenset({"filter_rows", "default_value", "null_if", "cast_loose"})

# Target types the compiler may emit. DECIMAL(p,s) is checked by regex below.
_SIMPLE_TYPES = {"VARCHAR", "INTEGER", "BIGINT", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP"}
_DECIMAL_RE = re.compile(r"^DECIMAL\((\d{1,2}),\s*(\d{1,2})\)$")

# Unit conversion factors to a base unit per dimension. Data sizes are binary (1 GB = 1024 MB),
# matching how RAM and VRAM are sold.
UNIT_FACTORS: dict[str, tuple[str, float]] = {
    "Hz": ("frequency", 1.0),
    "kHz": ("frequency", 1e3),
    "MHz": ("frequency", 1e6),
    "GHz": ("frequency", 1e9),
    "B": ("data", 1.0),
    "KB": ("data", 1024.0),
    "MB": ("data", 1024.0**2),
    "GB": ("data", 1024.0**3),
    "TB": ("data", 1024.0**4),
    "W": ("power", 1.0),
    "kW": ("power", 1e3),
    "mm": ("length", 1.0),
    "cm": ("length", 10.0),
    "in": ("length", 25.4),
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "lb": ("mass", 453.59237),
}

SUPPORTED_ENCODINGS = ("utf-8", "utf-16", "latin-1")


def normalize_type(value: str) -> str:
    """Upper-case and validate a target type name. Raises ValueError if not allowed."""
    t = " ".join(str(value).upper().split())
    if t in _SIMPLE_TYPES:
        return t
    m = _DECIMAL_RE.match(t)
    if m:
        precision, scale = int(m.group(1)), int(m.group(2))
        if not 1 <= precision <= 38 or scale > precision:
            raise ValueError(f"invalid DECIMAL precision/scale: {value!r}")
        return f"DECIMAL({precision},{scale})"
    raise ValueError(f"unsupported type {value!r}; allowed: {sorted(_SIMPLE_TYPES)} or DECIMAL(p,s)")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- transforms


class Trim(_Strict):
    op: Literal["trim"] = "trim"


class Lower(_Strict):
    op: Literal["lower"] = "lower"


class Upper(_Strict):
    op: Literal["upper"] = "upper"


class Replace(_Strict):
    """Literal (not regex) substring replacement."""

    op: Literal["replace"] = "replace"
    old: str = Field(min_length=1)
    new: str = ""


class RegexExtract(_Strict):
    """Extract a regex group. Strict: a non-empty input that does not match is an error."""

    op: Literal["regex_extract"] = "regex_extract"
    pattern: str = Field(min_length=1)
    group: int = Field(default=1, ge=0, le=9)

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            compiled = re.compile(v)
        except re.error as exc:
            raise ValueError(f"invalid regex {v!r}: {exc}") from exc
        if re.search(r"\(\?<?[=!]|\\[1-9]|\(\?P=", v):
            raise ValueError(f"regex {v!r} uses lookaround or backreferences (not in RE2)")
        if compiled.match(""):
            # a pattern that matches the empty string "succeeds" on any input
            raise ValueError(f"regex {v!r} matches the empty string")
        return v

    @model_validator(mode="after")
    def _group_exists(self) -> RegexExtract:
        if self.group > re.compile(self.pattern).groups:
            raise ValueError(f"regex {self.pattern!r} has no group {self.group}")
        return self


class ParseNumber(_Strict):
    """Parse a locale-formatted number to DOUBLE. Strict: unparseable input is an error."""

    op: Literal["parse_number"] = "parse_number"
    decimal: Literal[".", ","] = "."
    thousands: Literal[".", ",", " ", "'", "_"] | None = None

    @model_validator(mode="after")
    def _distinct(self) -> ParseNumber:
        if self.thousands == self.decimal:
            raise ValueError("thousands separator must differ from decimal separator")
        return self


class Scale(_Strict):
    """Multiply a numeric value by a constant (cents to euros, fixed FX rate, ...)."""

    op: Literal["scale"] = "scale"
    factor: float

    @field_validator("factor")
    @classmethod
    def _nonzero(cls, v: float) -> float:
        if v == 0:
            raise ValueError("scale factor 0 would erase the column")
        return v


class ConvertUnit(_Strict):
    """Convert between units of the same dimension, e.g. MHz to GHz or MB to GB."""

    op: Literal["convert_unit"] = "convert_unit"
    from_unit: str = Field(alias="from")
    to_unit: str = Field(alias="to")

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _compatible(self) -> ConvertUnit:
        for u in (self.from_unit, self.to_unit):
            if u not in UNIT_FACTORS:
                raise ValueError(f"unknown unit {u!r}; known: {sorted(UNIT_FACTORS)}")
        if UNIT_FACTORS[self.from_unit][0] != UNIT_FACTORS[self.to_unit][0]:
            raise ValueError(f"cannot convert {self.from_unit} to {self.to_unit}")
        return self

    @property
    def factor(self) -> float:
        return UNIT_FACTORS[self.from_unit][1] / UNIT_FACTORS[self.to_unit][1]


class MapValues(_Strict):
    """Code-table lookup. ``default``: error (strict), keep (pass through) or null."""

    op: Literal["map_values"] = "map_values"
    mapping: dict[str, str | int | float | bool]
    default: Literal["error", "keep", "null"] = "error"
    case_sensitive: bool = True

    @field_validator("default", mode="before")
    @classmethod
    def _yaml_null(cls, v: Any) -> Any:
        # `default: null` in YAML arrives as None
        return "null" if v is None else v

    @field_validator("mapping")
    @classmethod
    def _nonempty(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            raise ValueError("mapping must not be empty")
        return v


class ParseDate(_Strict):
    """Parse a date with a strptime format, e.g. '%d.%m.%Y'. Strict."""

    op: Literal["parse_date"] = "parse_date"
    format: str = Field(min_length=2)

    @field_validator("format")
    @classmethod
    def _has_directive(cls, v: str) -> str:
        if "%" not in v:
            raise ValueError("date format needs at least one % directive")
        return v


class Cast(_Strict):
    """Strict cast: a non-empty value that cannot be cast is an error."""

    op: Literal["cast"] = "cast"
    type: str

    @field_validator("type")
    @classmethod
    def _norm(cls, v: str) -> str:
        return normalize_type(v)


class CastLoose(_Strict):
    """RISKY. Cast that silently turns failures into NULL."""

    op: Literal["cast_loose"] = "cast_loose"
    type: str

    @field_validator("type")
    @classmethod
    def _norm(cls, v: str) -> str:
        return normalize_type(v)


class NullIf(_Strict):
    """RISKY. Turn the listed values into NULL (e.g. 'N/A'). Compared as trimmed text."""

    op: Literal["null_if"] = "null_if"
    values: list[str] = Field(min_length=1)


class DefaultValue(_Strict):
    """RISKY. Replace NULL with a constant."""

    op: Literal["default_value"] = "default_value"
    value: str | int | float | bool


Transform = Annotated[
    Trim
    | Lower
    | Upper
    | Replace
    | RegexExtract
    | ParseNumber
    | Scale
    | ConvertUnit
    | MapValues
    | ParseDate
    | Cast
    | CastLoose
    | NullIf
    | DefaultValue,
    Field(discriminator="op"),
]

# For the YAML shorthand ``- scale: 0.01`` : which field a bare scalar argument fills.
_PRIMARY_ARG = {
    "regex_extract": "pattern",
    "scale": "factor",
    "parse_date": "format",
    "cast": "type",
    "cast_loose": "type",
    "null_if": "values",
    "default_value": "value",
    "map_values": "mapping",
}
_NO_ARG_OPS = {"trim", "lower", "upper"}


def _expand_transform(raw: Any) -> Any:
    """Accept ``"trim"``, ``{"scale": 0.01}``, ``{"parse_number": {...}}`` or the long form."""
    if isinstance(raw, str):
        return {"op": raw}
    if isinstance(raw, dict) and "op" not in raw and len(raw) == 1:
        ((op, arg),) = raw.items()
        if arg is None or (op in _NO_ARG_OPS and arg in ({}, True)):
            return {"op": op}
        if isinstance(arg, dict) and op != "map_values":
            return {"op": op, **arg}
        if op == "map_values" and isinstance(arg, dict) and "mapping" in arg:
            return {"op": op, **arg}
        if op in _PRIMARY_ARG:
            if op == "null_if" and not isinstance(arg, list):
                arg = [arg]
            return {"op": op, _PRIMARY_ARG[op]: arg}
        raise ValueError(f"transform {op!r} needs a mapping of arguments, got {arg!r}")
    return raw


# --------------------------------------------------------------------------- row filter


class Condition(_Strict):
    """One structured predicate on a source column. No free-form expressions."""

    column: str = Field(min_length=1)
    op: Literal["eq", "ne", "lt", "le", "gt", "ge", "in", "not_in", "is_null", "not_null", "contains"]
    value: str | int | float | bool | list[str | int | float] | None = None

    @model_validator(mode="after")
    def _value_shape(self) -> Condition:
        if self.op in ("is_null", "not_null"):
            if self.value is not None:
                raise ValueError(f"{self.op} takes no value")
        elif self.op in ("in", "not_in"):
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"{self.op} needs a non-empty list")
        elif self.value is None or isinstance(self.value, list):
            raise ValueError(f"{self.op} needs a single value")
        return self


class FilterRows(_Strict):
    """RISKY. Keep only rows where every condition holds; the others are dropped."""

    keep_if: list[Condition] = Field(min_length=1)


# --------------------------------------------------------------------------- source + columns


class SourceOptions(_Strict):
    format: Literal["csv", "json"]
    encoding: Literal["utf-8", "utf-16", "latin-1"] = "utf-8"
    # csv
    delimiter: str = ","
    quote: str = '"'
    header: bool = True
    skip_rows: int = Field(default=0, ge=0)
    # json: dotted path to the array of records; None = top-level array or NDJSON
    record_path: str | None = None

    @field_validator("encoding", mode="before")
    @classmethod
    def _norm_encoding(cls, v: Any) -> Any:
        if isinstance(v, str):
            low = v.lower().replace("_", "-")
            aliases = {"utf8": "utf-8", "utf-8-sig": "utf-8", "latin1": "latin-1", "iso-8859-1": "latin-1", "utf16": "utf-16"}
            return aliases.get(low, low)
        return v

    @field_validator("delimiter", "quote")
    @classmethod
    def _single_char(cls, v: str) -> str:
        if len(v) != 1:
            raise ValueError("must be a single character")
        return v


class ColumnMapping(_Strict):
    """How one canonical column is produced. Exactly one of source / sources / const."""

    target: str = Field(min_length=1)
    source: str | None = None
    sources: list[str] | None = None
    sep: str = " "
    const: str | int | float | bool | None = None
    # only produce a value where every condition holds, NULL elsewhere. For feeds whose
    # generic columns mean different things per category (legacy ERP "A1".."A7").
    when: list[Condition] | None = None
    transforms: list[Transform] = Field(default_factory=list)
    note: str | None = None

    @field_validator("transforms", mode="before")
    @classmethod
    def _expand(cls, v: Any) -> Any:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("transforms must be a list")
        return [_expand_transform(t) for t in v]

    @model_validator(mode="after")
    def _one_input(self) -> ColumnMapping:
        given = [self.source is not None, bool(self.sources), self.const is not None]
        if sum(given) != 1:
            raise ValueError(f"column {self.target!r}: give exactly one of source, sources, const")
        if self.sources is not None and len(self.sources) < 2:
            raise ValueError(f"column {self.target!r}: sources needs at least two columns")
        return self

    def source_columns(self) -> list[str]:
        if self.source is not None:
            return [self.source]
        return list(self.sources or [])

    def condition_columns(self) -> list[str]:
        return [c.column for c in self.when or []]


class RiskyUse(BaseModel):
    model_config = ConfigDict(frozen=True)

    op: str
    target: str | None  # None for row-level filter_rows
    detail: str


class MappingSpec(_Strict):
    version: Literal[1] = SPEC_VERSION
    supplier: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    category: str | None = None
    source: SourceOptions
    filter_rows: FilterRows | None = None
    columns: list[ColumnMapping] = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _unique_targets(self) -> MappingSpec:
        seen: set[str] = set()
        for c in self.columns:
            if c.target in seen:
                raise ValueError(f"target column {c.target!r} is mapped twice")
            seen.add(c.target)
        return self

    # ---- introspection used by the router, gates and lineage

    def risky_ops(self) -> list[RiskyUse]:
        uses: list[RiskyUse] = []
        if self.filter_rows is not None:
            conds = ", ".join(f"{c.column} {c.op} {c.value!r}" for c in self.filter_rows.keep_if)
            uses.append(RiskyUse(op="filter_rows", target=None, detail=f"keep_if {conds}"))
        for col in self.columns:
            for t in col.transforms:
                if t.op in RISKY_OPS:
                    detail = t.model_dump_json(exclude={"op"}, by_alias=True)
                    uses.append(RiskyUse(op=t.op, target=col.target, detail=detail))
        return uses

    def lineage(self) -> dict[str, list[str]]:
        """Canonical column -> source columns its value is read from (``when`` columns excluded)."""
        return {c.target: c.source_columns() for c in self.columns}

    def source_columns(self) -> set[str]:
        cols = {s for c in self.columns for s in (*c.source_columns(), *c.condition_columns())}
        if self.filter_rows is not None:
            cols |= {c.column for c in self.filter_rows.keep_if}
        return cols

    def to_yaml(self) -> str:
        """Stable, diffable YAML (long-form transforms, no defaults)."""
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
        data = {"version": self.version, **data}
        for col, raw in zip(self.columns, data["columns"], strict=True):
            if col.transforms:
                raw["transforms"] = [
                    {t.op: t.model_dump(mode="json", by_alias=True, exclude={"op"})} if t.model_dump(exclude={"op"}) else t.op
                    for t in col.transforms
                ]
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


class _SpecLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans: only true/false are booleans.

    PyYAML follows YAML 1.1, where yes/no/on/off (and so codes like ``NO`` for Norway or a
    ``yes`` mapping value) silently become booleans. In a code table that is a bug.
    """


_SpecLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_SpecLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


def parse_spec(text: str) -> MappingSpec:
    data = yaml.load(text, Loader=_SpecLoader)  # noqa: S506 - SafeLoader subclass
    if not isinstance(data, dict):
        raise ValueError("mapping spec must be a YAML mapping")
    return MappingSpec.model_validate(data)


def load_spec(path: str | Path) -> MappingSpec:
    return parse_spec(Path(path).read_text(encoding="utf-8"))
