"""Compile a MappingSpec into one DuckDB SELECT.

Design notes

* The spec carries no SQL. Every identifier is quoted with ``quote_ident`` and every value
  with ``quote_literal``, so nothing in a spec can inject SQL.
* Transforms run as a chain of CTEs, one stage per transform position. Each stage
  references the previous stage by column name, so SQL size grows linearly with the
  pipeline length.
* Strict steps (parse_number, regex_extract, parse_date, cast, scale, convert_unit,
  map_values with ``default: error``, and the final cast to the target type) never fail
  the query. When a non-blank input becomes NULL they record ``"<target>:<op>"`` in
  ``_sg_errors``. Rows with errors are quarantined by the loader (``supplygate.loader``).
* The risky ops behave exactly as written: ``filter_rows`` drops rows, ``cast_loose``
  records nothing, ``null_if`` and ``default_value`` change values silently. Catching that
  is the job of the independent gates, not of the compiler.

Output columns: ``_sg_row_id`` (1-based source record number, for lineage and the
conservation check), every target column in target-schema order, then
``_sg_errors`` (VARCHAR[]).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from supplygate.mapping.spec import (
    Cast,
    CastLoose,
    ColumnMapping,
    Condition,
    ConvertUnit,
    DefaultValue,
    Lower,
    MappingSpec,
    MapValues,
    NullIf,
    ParseDate,
    ParseNumber,
    RegexExtract,
    Replace,
    Scale,
    Trim,
    Upper,
    normalize_type,
)

META_COLUMNS = ("_sg_row_id", "_sg_errors")

TargetSchema = Mapping[str, str]


class MappingCompileError(ValueError):
    """The spec cannot be compiled against the given target schema."""


class MissingSourceColumns(ValueError):
    def __init__(self, missing: list[str], available: list[str]):
        self.missing = missing
        self.available = available
        super().__init__(f"source columns not in feed: {missing}; feed has {available}")


# --------------------------------------------------------------------------- quoting


def quote_ident(name: str) -> str:
    if "\x00" in name:
        raise MappingCompileError("identifier contains NUL")
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise MappingCompileError(f"non-finite number {value!r}")
        return repr(value)
    s = str(value)
    if "\x00" in s:
        raise MappingCompileError("literal contains NUL")
    return "'" + s.replace("'", "''") + "'"


def _text_literal(value: Any) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    return quote_literal(str(value))


def _json_path(dotted: str) -> str:
    parts = [p for p in dotted.split(".") if p != ""]
    if not parts:
        raise MappingCompileError(f"empty JSON path {dotted!r}")
    return "$" + "".join('."' + p.replace("\\", "\\\\").replace('"', '\\"') + '"' for p in parts)


# --------------------------------------------------------------------------- step compilation

# value "kinds" tracked through a pipeline so text ops can cast their input
TEXT, NUMBER, DATE, BOOL, ANY = "text", "number", "date", "bool", "any"

_KIND_OF_TYPE = {
    "VARCHAR": TEXT,
    "INTEGER": NUMBER,
    "BIGINT": NUMBER,
    "DOUBLE": NUMBER,
    "BOOLEAN": BOOL,
    "DATE": DATE,
    "TIMESTAMP": DATE,
}


def _kind_of_type(t: str) -> str:
    return NUMBER if t.startswith("DECIMAL") else _KIND_OF_TYPE[t]


def _as_text(x: str, kind: str) -> str:
    return x if kind == TEXT else f"CAST({x} AS VARCHAR)"


# Exact arithmetic for prices: numbers are parsed to DECIMAL, and scaling by a factor that
# has an exact short decimal form stays in DECIMAL (54.50 * 1.17 = 63.765, not 63.76499...).
NUM_DECIMAL = "DECIMAL(38,10)"


def _as_number(x: str, kind: str) -> str:
    return x if kind == NUMBER else f"TRY_CAST({x} AS {NUM_DECIMAL})"


def _scale_expr(x: str, factor: float) -> str:
    d = Decimal(repr(factor))
    exponent = d.as_tuple().exponent
    if isinstance(exponent, int) and -exponent <= 10 and abs(d) < Decimal(10) ** 27:
        lit = f"CAST({quote_literal(format(d, 'f'))} AS {NUM_DECIMAL})"
        return f"CAST({x} * {lit} AS {NUM_DECIMAL})"
    # factors like 1/1024**3 need more than 10 decimals: fall back to DOUBLE
    return f"(CAST({x} AS DOUBLE) * {quote_literal(float(factor))})"


def _strip_ws(x: str) -> str:
    # trims ASCII whitespace plus NBSP / narrow NBSP, which supplier exports love
    return f"regexp_replace({x}, '^[\\s\\x{{00A0}}\\x{{202F}}]+|[\\s\\x{{00A0}}\\x{{202F}}]+$', '', 'g')"


@dataclass(frozen=True)
class _Step:
    expr: str
    kind: str
    strict: bool


def _compile_step(t: Any, x: str, kind: str) -> _Step:
    """Compile one transform applied to column reference ``x`` of the given kind."""
    if isinstance(t, Trim):
        return _Step(_strip_ws(_as_text(x, kind)), TEXT, False)
    if isinstance(t, Lower):
        return _Step(f"lower({_as_text(x, kind)})", TEXT, False)
    if isinstance(t, Upper):
        return _Step(f"upper({_as_text(x, kind)})", TEXT, False)
    if isinstance(t, Replace):
        return _Step(
            f"replace({_as_text(x, kind)}, {quote_literal(t.old)}, {quote_literal(t.new)})",
            TEXT,
            False,
        )
    if isinstance(t, RegexExtract):
        e = f"NULLIF(regexp_extract({_as_text(x, kind)}, {quote_literal(t.pattern)}, {t.group}), '')"
        return _Step(e, TEXT, True)
    if isinstance(t, ParseNumber):
        # DECIMAL(38,10), not DOUBLE, so money keeps exact cents
        e = _strip_ws(_as_text(x, kind))
        if t.thousands == " ":
            e = f"regexp_replace({e}, '[\\s\\x{{00A0}}\\x{{202F}}]', '', 'g')"
        elif t.thousands is not None:
            e = f"replace({e}, {quote_literal(t.thousands)}, '')"
        if t.decimal == ",":
            e = f"replace({e}, ',', '.')"
        return _Step(f"TRY_CAST({e} AS {NUM_DECIMAL})", NUMBER, True)
    if isinstance(t, (Scale, ConvertUnit)):
        return _Step(_scale_expr(_as_number(x, kind), float(t.factor)), NUMBER, True)
    if isinstance(t, MapValues):
        return _compile_map_values(t, x, kind)
    if isinstance(t, ParseDate):
        e = f"CAST(try_strptime({_strip_ws(_as_text(x, kind))}, {quote_literal(t.format)}) AS DATE)"
        return _Step(e, DATE, True)
    if isinstance(t, Cast):
        return _Step(f"TRY_CAST({x} AS {t.type})", _kind_of_type(t.type), True)
    if isinstance(t, CastLoose):
        return _Step(f"TRY_CAST({x} AS {t.type})", _kind_of_type(t.type), False)
    if isinstance(t, NullIf):
        vals = ", ".join(quote_literal(v.strip()) for v in t.values)
        e = f"CASE WHEN {_strip_ws(_as_text(x, kind))} IN ({vals}) THEN NULL ELSE {x} END"
        return _Step(e, kind, False)
    if isinstance(t, DefaultValue):
        lit = _text_literal(t.value) if kind in (TEXT, ANY) else quote_literal(t.value)
        return _Step(f"COALESCE({x}, {lit})", kind, False)
    raise MappingCompileError(f"no compiler for transform {t!r}")  # pragma: no cover


def _compile_map_values(t: MapValues, x: str, kind: str) -> _Step:
    key = _strip_ws(_as_text(x, kind))
    if not t.case_sensitive:
        key = f"lower({key})"
    values = list(t.mapping.values())
    if t.default == "keep" or not _homogeneous(values):
        out_kind, render = TEXT, _text_literal
    elif all(isinstance(v, bool) for v in values):
        out_kind, render = BOOL, quote_literal
    elif all(isinstance(v, (int, float)) for v in values):
        out_kind, render = NUMBER, quote_literal
    else:
        out_kind, render = TEXT, _text_literal
    whens = " ".join(f"WHEN {quote_literal(k if t.case_sensitive else k.lower())} THEN {render(v)}" for k, v in t.mapping.items())
    default = {"keep": _as_text(x, kind), "null": "NULL", "error": "NULL"}[t.default]
    return _Step(f"CASE {key} {whens} ELSE {default} END", out_kind, t.default == "error")


def _homogeneous(values: list[Any]) -> bool:
    if all(isinstance(v, bool) for v in values):
        return True
    if any(isinstance(v, bool) for v in values):
        return False
    return all(isinstance(v, (int, float)) for v in values) or all(isinstance(v, str) for v in values)


def _fail_predicate(prev: str, new: str) -> str:
    return f"({prev} IS NOT NULL AND trim(CAST({prev} AS VARCHAR)) <> '' AND ({new}) IS NULL)"


# --------------------------------------------------------------------------- source + filter


def _source_cte(spec: MappingSpec, path: str) -> tuple[str, Any]:
    """Returns (SQL for the raw records, column-ref function)."""
    src = spec.source
    p = quote_literal(path)
    if src.format == "csv":
        opts = [
            f"delim={quote_literal(src.delimiter)}",
            f"quote={quote_literal(src.quote)}",
            f"header={'true' if src.header else 'false'}",
            f"skip={src.skip_rows}",
            f"encoding={quote_literal(src.encoding)}",
            "all_varchar=true",
        ]
        sql = f"SELECT row_number() OVER () AS _sg_row_id, * FROM read_csv({p}, {', '.join(opts)})"
        return sql, quote_ident
    # json
    if src.encoding != "utf-8":
        raise MappingCompileError("JSON sources must be UTF-8")
    if src.record_path:
        inner = (
            f"SELECT unnest(CAST(json_extract(json, {quote_literal(_json_path(src.record_path))}) "
            f"AS JSON[])) AS _sg_rec FROM read_json_objects({p}, format='auto')"
        )
    else:
        inner = f"SELECT json AS _sg_rec FROM read_json_objects({p}, format='auto')"
    sql = f"SELECT row_number() OVER () AS _sg_row_id, _sg_rec FROM ({inner})"
    return sql, lambda c: f"json_extract_string(_sg_rec, {quote_literal(_json_path(c))})"


def _condition_sql(c: Condition, ref: Any) -> str:
    col = ref(c.column)
    if c.op == "is_null":
        return f"({col} IS NULL OR trim({col}) = '')"
    if c.op == "not_null":
        return f"({col} IS NOT NULL AND trim({col}) <> '')"
    if c.op in ("in", "not_in"):
        vals = ", ".join(_text_literal(v) for v in c.value)  # type: ignore[union-attr]
        neg = "NOT " if c.op == "not_in" else ""
        return f"({col} {neg}IN ({vals}))"
    if c.op == "contains":
        return f"contains({col}, {_text_literal(c.value)})"
    sym = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[c.op]
    if isinstance(c.value, (int, float)) and not isinstance(c.value, bool):
        return f"(TRY_CAST({col} AS DOUBLE) {sym} {quote_literal(c.value)})"
    return f"({col} {sym} {_text_literal(c.value)})"


# --------------------------------------------------------------------------- public API


@dataclass(frozen=True)
class CompiledMapping:
    spec: MappingSpec
    target_schema: dict[str, str]
    sql: str
    source_path: str
    output_columns: list[str] = field(default_factory=list)


def _normalize_schema(target_schema: TargetSchema) -> dict[str, str]:
    if not target_schema:
        raise MappingCompileError("target schema is empty")
    out: dict[str, str] = {}
    for name, typ in target_schema.items():
        if name in META_COLUMNS:
            raise MappingCompileError(f"{name!r} is reserved for SupplyGate metadata")
        try:
            out[name] = normalize_type(typ)
        except ValueError as exc:
            raise MappingCompileError(f"target column {name!r}: {exc}") from exc
    return out


def _initial_expr(col: ColumnMapping, ref: Any) -> tuple[str, str]:
    if col.source is not None:
        return ref(col.source), TEXT
    if col.sources:
        parts = ", ".join(ref(s) for s in col.sources)
        # concat_ws skips NULLs; NULLIF turns an all-NULL concat into NULL
        return f"NULLIF(concat_ws({quote_literal(col.sep)}, {parts}), '')", TEXT
    v = col.const
    if isinstance(v, bool):
        return quote_literal(v), BOOL
    if isinstance(v, (int, float)):
        return quote_literal(v), NUMBER
    return quote_literal(v), TEXT


def compile_spec(spec: MappingSpec, source_path: str | Path, target_schema: TargetSchema) -> CompiledMapping:
    """Compile ``spec`` for one feed file into a single DuckDB SELECT statement."""
    schema = _normalize_schema(target_schema)
    unknown = [c.target for c in spec.columns if c.target not in schema]
    if unknown:
        raise MappingCompileError(f"spec maps to columns not in the target schema: {unknown}")

    path = Path(source_path).as_posix()
    src_sql, ref = _source_cte(spec, path)
    ctes: list[str] = [f"src AS ({src_sql})"]

    passthrough = "*" if spec.source.format == "csv" else "_sg_row_id, _sg_rec"
    where = "TRUE"
    if spec.filter_rows is not None:
        where = " AND ".join(f"COALESCE({_condition_sql(c, ref)}, FALSE)" for c in spec.filter_rows.keep_if)
    ctes.append(f"kept AS (SELECT {passthrough} FROM src WHERE {where})")

    # stage 0: raw input per mapped column; v{i} = value, e{i} = first failing op
    cols = spec.columns
    kinds: list[str] = []
    stage0 = ["_sg_row_id"]
    for i, col in enumerate(cols):
        expr, kind = _initial_expr(col, ref)
        if col.when:
            cond = " AND ".join(f"COALESCE({_condition_sql(c, ref)}, FALSE)" for c in col.when)
            expr = f"CASE WHEN {cond} THEN {expr} END"
        kinds.append(kind)
        stage0.append(f"{expr} AS v{i}")
        stage0.append(f"CAST(NULL AS VARCHAR) AS e{i}")
    ctes.append(f"s0 AS (SELECT {', '.join(stage0)} FROM kept)")

    depth = max((len(c.transforms) for c in cols), default=0)
    for k in range(depth):
        sel = ["_sg_row_id"]
        for i, col in enumerate(cols):
            v, e = f"v{i}", f"e{i}"
            if k < len(col.transforms):
                t = col.transforms[k]
                step = _compile_step(t, v, kinds[i])
                kinds[i] = step.kind
                sel.append(f"{step.expr} AS {v}")
                if step.strict:
                    fail = _fail_predicate(v, step.expr)
                    sel.append(f"COALESCE({e}, CASE WHEN {fail} THEN {quote_literal(t.op)} END) AS {e}")
                else:
                    sel.append(e)
            else:
                sel.extend([v, e])
        ctes.append(f"s{k + 1} AS (SELECT {', '.join(sel)} FROM s{k})")

    # final projection: strict cast to the target type, collect errors
    by_target = {c.target: i for i, c in enumerate(cols)}
    final = ["_sg_row_id"]
    error_items = []
    last = f"s{depth}"
    for name, typ in schema.items():
        if name not in by_target:
            final.append(f"CAST(NULL AS {typ}) AS {quote_ident(name)}")
            continue
        i = by_target[name]
        cast = f"TRY_CAST(v{i} AS {typ})"
        final.append(f"{cast} AS {quote_ident(name)}")
        fail = _fail_predicate(f"v{i}", cast)
        tgt = quote_literal(name + ":")
        error_items.append(f"CASE WHEN e{i} IS NOT NULL THEN {tgt} || e{i} WHEN {fail} THEN {quote_literal(name + ':cast_target')} END")
    if error_items:
        # list_distinct drops NULLs; no lambda, so it works across DuckDB lambda-syntax changes
        final.append(f"CAST(list_sort(list_distinct([{', '.join(error_items)}])) AS VARCHAR[]) AS _sg_errors")
    else:
        final.append("CAST([] AS VARCHAR[]) AS _sg_errors")

    sql = "WITH " + ",\n".join(ctes) + f"\nSELECT {', '.join(final)}\nFROM {last}\nORDER BY _sg_row_id"
    out_cols = ["_sg_row_id", *schema.keys(), "_sg_errors"]
    return CompiledMapping(spec=spec, target_schema=schema, sql=sql, source_path=path, output_columns=out_cols)


def feed_columns(con: duckdb.DuckDBPyConnection, spec: MappingSpec, source_path: str | Path) -> list[str]:
    """Header names of a CSV feed as DuckDB reads them with the spec's source options."""
    src_sql, _ = _source_cte(spec, Path(source_path).as_posix())
    rows = con.execute(f"DESCRIBE SELECT * FROM ({src_sql})").fetchall()
    return [r[0] for r in rows if r[0] != "_sg_row_id"]


def run_mapping(
    con: duckdb.DuckDBPyConnection,
    spec: MappingSpec,
    source_path: str | Path,
    target_schema: TargetSchema,
    *,
    check_columns: bool = True,
) -> duckdb.DuckDBPyRelation:
    """Compile and run. For CSV feeds, missing source columns raise MissingSourceColumns."""
    compiled = compile_spec(spec, source_path, target_schema)
    if check_columns and spec.source.format == "csv":
        available = feed_columns(con, spec, source_path)
        missing = sorted(spec.source_columns() - set(available))
        if missing:
            raise MissingSourceColumns(missing, available)
    return con.sql(compiled.sql)
