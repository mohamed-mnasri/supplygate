"""Spec loading and validation: short forms, round-tripping, risky-op listing, and the specs that must be rejected."""

import pytest
from pydantic import ValidationError

from supplygate.mapping.spec import MappingSpec, parse_spec

BASE = """
version: 1
supplier: de_csv
source: {format: csv, delimiter: ";"}
"""


def spec(columns_yaml: str, extra: str = "") -> MappingSpec:
    return parse_spec(BASE + extra + "columns:\n" + columns_yaml)


def test_shorthand_and_long_form_are_equivalent():
    short = spec("""
  - target: price_eur
    source: Preis
    transforms:
      - trim
      - parse_number: {decimal: ",", thousands: "."}
      - scale: 0.01
      - null_if: "N/A"
      - cast: integer
""")
    long = spec("""
  - target: price_eur
    source: Preis
    transforms:
      - {op: trim}
      - {op: parse_number, decimal: ",", thousands: "."}
      - {op: scale, factor: 0.01}
      - {op: null_if, values: ["N/A"]}
      - {op: cast, type: INTEGER}
""")
    assert short == long
    assert short.columns[0].transforms[-1].type == "INTEGER"


def test_to_yaml_round_trips():
    s = spec(
        """
  - {target: supplier_id, const: de_csv}
  - target: cpu_base_clock_mhz
    source: Takt
    transforms:
      - regex_extract: '([0-9,]+)\\s*GHz'
      - parse_number: {decimal: ","}
      - convert_unit: {from: GHz, to: MHz}
  - target: category
    source: Typ
    transforms: [{map_values: {CPU: cpu, GPU: gpu}}, {default_value: cpu}]
  - {target: model_name, sources: [Serie, Modell], sep: " "}
  - {target: cpu_cores, source: A2, when: [{column: WGR, op: in, value: [100, 101]}]}
""",
        extra="filter_rows: {keep_if: [{column: Status, op: ne, value: EOL}]}\n",
    )
    assert parse_spec(s.to_yaml()) == s


def test_risky_ops_are_listed_with_location():
    s = spec(
        """
  - {target: stock_qty, source: Lager, transforms: [{default_value: 0}]}
  - {target: price_eur, source: Preis, transforms: [{null_if: ["-", "N/A"]}, {cast_loose: DOUBLE}]}
  - {target: brand, source: Marke, transforms: [trim]}
""",
        extra="filter_rows: {keep_if: [{column: Preis, op: gt, value: 0}]}\n",
    )
    used = [(r.op, r.target) for r in s.risky_ops()]
    assert used == [
        ("filter_rows", None),
        ("default_value", "stock_qty"),
        ("null_if", "price_eur"),
        ("cast_loose", "price_eur"),
    ]


def test_lineage_and_source_columns():
    s = spec(
        """
  - {target: model_name, sources: [Serie, Modell]}
  - {target: supplier_id, const: x}
""",
        extra="filter_rows: {keep_if: [{column: Status, op: not_null}]}\n",
    )
    assert s.lineage() == {"model_name": ["Serie", "Modell"], "supplier_id": []}
    assert s.source_columns() == {"Serie", "Modell", "Status"}


@pytest.mark.parametrize(
    "columns, message",
    [
        ("  - {target: a, source: X, transforms: [{exec: 'rm -rf /'}]}", "exec"),
        ("  - {target: a, source: X, transforms: [{cast: TEXT}]}", "unsupported type"),
        ("  - {target: a, source: X, transforms: [{cast: 'INTEGER; DROP TABLE t'}]}", "unsupported type"),
        ("  - {target: a, source: X, transforms: [{regex_extract: '(['}]}", "invalid regex"),
        ("  - {target: a, source: X, transforms: [{regex_extract: '(?=x)(a)'}]}", "lookaround"),
        ("  - {target: a, source: X, transforms: [{regex_extract: '(\\d*)'}]}", "empty string"),
        ("  - {target: a, source: X, transforms: [{regex_extract: {pattern: '(a)', group: 2}}]}", "no group 2"),
        ("  - {target: a, source: X, transforms: [{convert_unit: {from: GHz, to: GB}}]}", "cannot convert"),
        ("  - {target: a, source: X, transforms: [{convert_unit: {from: GHz, to: parsec}}]}", "unknown unit"),
        ("  - {target: a, source: X, transforms: [{parse_number: {decimal: ',', thousands: ','}}]}", "must differ"),
        ("  - {target: a, source: X, transforms: [{scale: 0}]}", "erase"),
        ("  - {target: a, source: X, transforms: [{map_values: {}}]}", "must not be empty"),
        ("  - {target: a, source: X, transforms: [{parse_date: 'dd.mm.yyyy'}]}", "directive"),
        ("  - {target: a, source: X, const: 1}", "exactly one"),
        ("  - {target: a}", "exactly one"),
        ("  - {target: a, sources: [X]}", "at least two"),
        ("  - {target: a, source: X, sql: 'select 1'}", "Extra inputs"),
        ("  - {target: a, source: X}\n  - {target: a, source: Y}", "mapped twice"),
    ],
)
def test_invalid_specs_are_rejected(columns, message):
    with pytest.raises(ValidationError, match=message):
        spec(columns)


def test_bad_filter_shapes_are_rejected():
    for cond in ("{column: X, op: in, value: a}", "{column: X, op: is_null, value: 1}", "{column: X, op: eq}"):
        with pytest.raises(ValidationError):
            spec("  - {target: a, source: X}", extra=f"filter_rows: {{keep_if: [{cond}]}}\n")


def test_supplier_id_and_source_options_are_validated():
    with pytest.raises(ValidationError):
        parse_spec("supplier: 'DE CSV'\nsource: {format: csv}\ncolumns: [{target: a, source: X}]")
    with pytest.raises(ValidationError):
        parse_spec("supplier: de\nsource: {format: csv, delimiter: ';;'}\ncolumns: [{target: a, source: X}]")
    with pytest.raises(ValidationError):
        parse_spec("supplier: de\nsource: {format: csv, encoding: shift-jis}\ncolumns: [{target: a, source: X}]")
    s = parse_spec("supplier: fr\nsource: {format: csv, encoding: ISO-8859-1}\ncolumns: [{target: a, source: X}]")
    assert s.source.encoding == "latin-1"


def test_yaml_11_booleans_stay_strings():
    s = spec("  - {target: a, source: X, transforms: [{map_values: {NO: Norway, yes: oui, on: an, x: true}}]}")
    assert s.columns[0].transforms[0].mapping == {"NO": "Norway", "yes": "oui", "on": "an", "x": True}
