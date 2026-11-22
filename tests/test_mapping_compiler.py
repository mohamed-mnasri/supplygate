"""Compiler behaviour, run end to end in DuckDB on small feeds written by each test."""

from pathlib import Path

import duckdb
import pytest

from supplygate.mapping import compile_spec, run_mapping
from supplygate.mapping.compiler import MappingCompileError, MissingSourceColumns
from supplygate.mapping.spec import parse_spec
from supplygate.schema import canonical_columns

FEEDS = Path(__file__).parent / "fixtures" / "feeds"
SPECS = Path(__file__).parent / "fixtures" / "specs"

SMALL = {"sku": "VARCHAR", "price_eur": "DECIMAL(10,2)", "qty": "INTEGER", "note": "VARCHAR"}


@pytest.fixture
def con():
    c = duckdb.connect()
    yield c
    c.close()


def write_csv(tmp_path: Path, text: str, name: str = "feed.csv", encoding: str = "utf-8") -> Path:
    p = tmp_path / name
    p.write_bytes(text.encode(encoding))
    return p


def run(con, spec_yaml: str, path: Path, schema=SMALL) -> list[dict]:
    rel = run_mapping(con, parse_spec(spec_yaml), path, schema)
    cols = rel.columns
    return [dict(zip(cols, row, strict=True)) for row in rel.fetchall()]


CSV_SPEC = """
supplier: t
source: {{format: csv, delimiter: ";"}}
{filter}
columns:
{columns}
"""


def csv_spec(columns: str, filter_: str = "") -> str:
    return CSV_SPEC.format(columns=columns, filter=filter_)


# --------------------------------------------------------------------------- end to end


def test_german_feed_against_canonical_schema(con):
    spec_path = SPECS / "de_sample.yaml"
    from supplygate.mapping import load_spec

    rel = run_mapping(con, load_spec(spec_path), FEEDS / "de_sample.csv", canonical_columns())
    rows = [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]
    assert rel.columns[0] == "_sg_row_id" and rel.columns[-1] == "_sg_errors"
    assert rel.columns[1:-1] == list(canonical_columns())
    assert [r["_sg_row_id"] for r in rows] == [1, 2, 3]
    r0, r2 = rows[0], rows[2]
    assert r0["supplier_id"] == "de_csv" and r0["category"] == "cpu"
    assert str(r0["price_eur"]) == "1299.50"
    assert r0["cpu_base_clock_mhz"] == 4200
    assert r2["category"] == "gpu" and r2["gpu_vram_gb"] == 12 and r2["cpu_base_clock_mhz"] is None
    assert all(r["_sg_errors"] == [] for r in rows)


def test_output_can_be_inserted_into_canonical_table(con):
    from supplygate import schema
    from supplygate.mapping import load_spec

    schema.create_tables(con)
    compiled = compile_spec(load_spec(SPECS / "de_sample.yaml"), FEEDS / "de_sample.csv", canonical_columns())
    cols = ", ".join(canonical_columns())
    con.execute(f"INSERT INTO {schema.TABLE_NAME} SELECT {cols} FROM ({compiled.sql}) WHERE len(_sg_errors) = 0")
    assert con.execute(f"SELECT count(*) FROM {schema.TABLE_NAME}").fetchone() == (3,)


def test_latin1_french_feed(con):
    spec = """
supplier: fr_csv
source: {format: csv, delimiter: ";", encoding: latin-1}
columns:
  - {target: sku, source: Référence}
  - target: price_eur
    source: Prix TTC
    transforms: [{null_if: N/A}, {parse_number: {decimal: ","}}]
  - target: qty
    source: Mémoire vidéo
    transforms: [{regex_extract: '(\\d+)\\s*Go'}, {cast: INTEGER}]
  - target: note
    source: Disponibilité
    transforms: [{map_values: {oui: in_stock, non: out_of_stock}}]
"""
    rows = run(con, spec, FEEDS / "fr_sample.csv")
    assert [r["qty"] for r in rows] == [8, 16, 8]
    assert [r["note"] for r in rows] == ["in_stock", "out_of_stock", "in_stock"]
    assert rows[2]["price_eur"] is None and rows[2]["_sg_errors"] == []  # hidden by null_if


def test_uk_feed_currency_and_thousands(con):
    spec = """
supplier: uk_csv
source: {format: csv}
columns:
  - {target: sku, source: sku}
  - target: price_eur
    source: price_gbp
    transforms: [{replace: {old: "£"}}, {parse_number: {thousands: ","}}, {scale: 1.17}]
  - target: qty
    source: speed
    transforms: [{regex_extract: '(\\d+)\\s*MHz'}, {cast: INTEGER}]
"""
    rows = run(con, spec, FEEDS / "uk_sample.csv")
    assert [str(r["price_eur"]) for r in rows] == ["1228.49", "63.77"]
    assert [r["qty"] for r in rows] == [6000, 3200]


def test_nested_json_with_record_path(con):
    spec = """
supplier: json_api
source: {format: json, record_path: data.items}
columns:
  - {target: sku, source: id}
  - {target: price_eur, source: pricing.amount_cents, transforms: [{scale: 0.01}]}
  - {target: qty, source: specs.cores, transforms: [{cast: INTEGER}]}
  - {target: note, sources: [brand, title]}
"""
    rows = run(con, spec, FEEDS / "json_sample.json")
    assert [(r["sku"], str(r["price_eur"]), r["qty"], r["note"]) for r in rows] == [
        ("J1", "219.99", 6, "AMD Ryzen 5 7600"),
        ("J2", "409.00", 20, "Intel Core i7-14700K"),
    ]
    assert [r["_sg_row_id"] for r in rows] == [1, 2]


def test_top_level_json_array(con, tmp_path):
    p = tmp_path / "a.json"
    p.write_text('[{"sku": "a", "p": "1.5"}, {"sku": "b", "p": null}]')
    spec = "supplier: j\nsource: {format: json}\ncolumns: [{target: sku, source: sku}, {target: price_eur, source: p}]"
    rows = run(con, spec, p)
    assert [(r["sku"], r["price_eur"] and str(r["price_eur"])) for r in rows] == [("a", "1.50"), ("b", None)]


# --------------------------------------------------------------------------- strict errors


def test_strict_failures_are_recorded_not_raised(con, tmp_path):
    p = write_csv(tmp_path, "sku;price;qty\na;12,50;3\nb;abc;x\nc;;\n")
    spec = csv_spec("""
  - {target: sku, source: sku}
  - {target: price_eur, source: price, transforms: [{parse_number: {decimal: ","}}]}
  - {target: qty, source: qty}
""")
    rows = run(con, spec, p)
    assert rows[0]["_sg_errors"] == []
    assert rows[1]["_sg_errors"] == ["price_eur:parse_number", "qty:cast_target"]
    assert rows[1]["price_eur"] is None
    # blanks are missing values, not errors
    assert rows[2]["_sg_errors"] == [] and rows[2]["qty"] is None


def test_forgetting_number_parsing_is_caught_by_target_cast(con, tmp_path):
    p = write_csv(tmp_path, "sku;price\na;1.299,50\n")
    rows = run(con, csv_spec("  - {target: sku, source: sku}\n  - {target: price_eur, source: price}"), p)
    assert rows[0]["price_eur"] is None
    assert rows[0]["_sg_errors"] == ["price_eur:cast_target"]


def test_first_failing_step_is_reported(con, tmp_path):
    p = write_csv(tmp_path, "sku;speed\na;fast\n")
    spec = csv_spec("""
  - target: qty
    source: speed
    transforms: [{regex_extract: '(\\d+)'}, {cast: INTEGER}, {scale: 2}]
""")
    assert run(con, spec, p)[0]["_sg_errors"] == ["qty:regex_extract"]


def test_map_values_default_modes(con, tmp_path):
    p = write_csv(tmp_path, "sku;s\na;J\nb;N\nc;?\n")
    for default, expected_errors in [
        ("error", [[], [], ["qty:map_values"]]),
        ("null", [[], [], []]),
        ("~", [[], [], []]),  # YAML null means "null"
    ]:
        spec = csv_spec(f"  - {{target: qty, source: s, transforms: [{{map_values: {{mapping: {{J: 1, N: 0}}, default: {default}}}}}]}}")
        rows = run(con, spec, p)
        assert [r["qty"] for r in rows] == [1, 0, None]
        assert [r["_sg_errors"] for r in rows] == expected_errors
    spec = csv_spec("  - {target: note, source: s, transforms: [{map_values: {mapping: {j: yes}, default: keep, case_sensitive: false}}]}")
    assert [r["note"] for r in run(con, spec, p)] == ["yes", "N", "?"]


def test_parse_date_and_trim(con, tmp_path):
    p = write_csv(tmp_path, "d\n 24.12.2025 \n31.02.2025\n")
    schema = {"release": "DATE"}
    spec = "supplier: t\nsource: {format: csv}\ncolumns: [{target: release, source: d, transforms: [{parse_date: '%d.%m.%Y'}]}]"
    rows = run(con, spec, p, schema)
    assert str(rows[0]["release"]) == "2025-12-24"
    assert rows[1]["release"] is None and rows[1]["_sg_errors"] == ["release:parse_date"]


# --------------------------------------------------------------------------- risky ops


def test_cast_loose_swallows_what_cast_reports(con, tmp_path):
    p = write_csv(tmp_path, "sku;qty\na;12\nb;twelve\n")
    strict = run(con, csv_spec("  - {target: qty, source: qty, transforms: [{cast: INTEGER}]}"), p)
    loose = run(con, csv_spec("  - {target: qty, source: qty, transforms: [{cast_loose: INTEGER}]}"), p)
    assert strict[1]["_sg_errors"] == ["qty:cast"]
    assert loose[1]["qty"] is None and loose[1]["_sg_errors"] == []


def test_default_value_fills_missing(con, tmp_path):
    p = write_csv(tmp_path, "sku;qty\na;\nb;5\n")
    rows = run(con, csv_spec("  - {target: qty, source: qty, transforms: [{cast: INTEGER}, {default_value: 0}]}"), p)
    assert [r["qty"] for r in rows] == [0, 5]
    rows = run(con, csv_spec("  - {target: note, source: qty, transforms: [{default_value: unknown}]}"), p)
    assert [r["note"] for r in rows] == ["unknown", "5"]


def test_filter_rows_drops_rows_and_keeps_row_ids(con, tmp_path):
    p = write_csv(tmp_path, "sku;price;status\na;10;ok\nb;0;ok\nc;20;EOL\nd;30;\n")
    spec = csv_spec(
        "  - {target: sku, source: sku}",
        filter_="filter_rows: {keep_if: [{column: price, op: gt, value: 0}, {column: status, op: ne, value: EOL}]}",
    )
    rows = run(con, spec, p)
    # 'd' has NULL status: ne on NULL is unknown, and unknown drops the row
    assert [(r["_sg_row_id"], r["sku"]) for r in rows] == [(1, "a")]


@pytest.mark.parametrize(
    "cond, kept",
    [
        ("{column: s, op: in, value: [x, y]}", ["1", "2"]),
        ("{column: s, op: not_in, value: [x]}", ["2", "3"]),
        ("{column: s, op: is_null}", ["4"]),
        ("{column: s, op: not_null}", ["1", "2", "3"]),
        ("{column: s, op: contains, value: z}", ["3"]),
        ("{column: n, op: le, value: 2}", ["1", "2"]),
        ("{column: s, op: eq, value: y}", ["2"]),
    ],
)
def test_filter_conditions(con, tmp_path, cond, kept):
    p = write_csv(tmp_path, "sku;s;n\n1;x;1\n2;y;2\n3;zz;3\n4;;4\n")
    spec = csv_spec("  - {target: sku, source: sku}", filter_=f"filter_rows: {{keep_if: [{cond}]}}")
    assert [r["sku"] for r in run(con, spec, p)] == kept


# --------------------------------------------------------------------------- safety + errors


def test_hostile_names_and_values_stay_data(con, tmp_path):
    evil_col = 'x"; DROP TABLE t; --'
    p = write_csv(tmp_path, f'sku;"{evil_col.replace(chr(34), chr(34) * 2)}"\na;it\'s\n')
    con.execute("CREATE TABLE t (i INTEGER)")
    spec = parse_spec(
        "supplier: t\nsource: {format: csv, delimiter: ';'}\ncolumns:\n"
        f"  - target: note\n    source: '{evil_col}'\n"
        '    transforms: [{replace: {old: "\'", new: "\'); DROP TABLE t; --"}}, {default_value: "\'"}]\n'
    )
    rows = run_mapping(con, spec, p, SMALL).fetchall()
    assert rows[0][4] == "it'); DROP TABLE t; --s"
    assert con.execute("SELECT count(*) FROM t").fetchone() == (0,)


def test_unknown_target_column_is_a_compile_error(tmp_path):
    spec = parse_spec(csv_spec("  - {target: vram_gb, source: x}"))
    with pytest.raises(MappingCompileError, match="vram_gb"):
        compile_spec(spec, tmp_path / "f.csv", canonical_columns())


def test_bad_target_schema_is_rejected(tmp_path):
    spec = parse_spec(csv_spec("  - {target: sku, source: x}"))
    with pytest.raises(MappingCompileError, match="unsupported type"):
        compile_spec(spec, tmp_path / "f.csv", {"sku": "VARCHAR(10); DROP"})
    with pytest.raises(MappingCompileError, match="reserved"):
        compile_spec(spec, tmp_path / "f.csv", {"sku": "VARCHAR", "_sg_errors": "VARCHAR"})


def test_missing_source_columns_are_reported(con, tmp_path):
    p = write_csv(tmp_path, "sku;price\na;1\n")
    spec = parse_spec(csv_spec("  - {target: sku, source: SKU}\n  - {target: price_eur, source: price}"))
    with pytest.raises(MissingSourceColumns) as exc:
        run_mapping(con, spec, p, SMALL)
    assert exc.value.missing == ["SKU"]
    assert exc.value.available == ["sku", "price"]


def test_unmapped_targets_are_null_and_typed(con, tmp_path):
    p = write_csv(tmp_path, "sku\na\n")
    rel = run_mapping(con, parse_spec(csv_spec("  - {target: sku, source: sku}")), p, SMALL)
    types = dict(zip(rel.columns, [str(t) for t in rel.types], strict=True))
    assert types["price_eur"] == "DECIMAL(10,2)" and types["qty"] == "INTEGER"
    assert types["_sg_errors"] == "VARCHAR[]"
    assert rel.fetchall() == [(1, "a", None, None, None, [])]


def test_long_pipeline_sql_grows_linearly(tmp_path):
    steps = ", ".join(["trim"] * 40)
    spec = parse_spec(csv_spec(f"  - {{target: note, source: s, transforms: [{steps}]}}"))
    assert len(compile_spec(spec, tmp_path / "f.csv", SMALL).sql) < 20_000


def test_when_selects_rows_per_category(con, tmp_path):
    p = write_csv(tmp_path, "sku;grp;a2\nc1;100;8\ng1;200;12288\nr1;300;abc\n")
    spec = csv_spec("""
  - {target: sku, source: sku}
  - target: qty
    source: a2
    when: [{column: grp, op: eq, value: 100}]
    transforms: [{cast: INTEGER}]
  - target: price_eur
    source: a2
    when: [{column: grp, op: in, value: [200]}]
    transforms: [{convert_unit: {from: MB, to: GB}}]
""")
    rows = run(con, spec, p)
    assert [(r["qty"], r["price_eur"] and int(r["price_eur"])) for r in rows] == [(8, None), (None, 12), (None, None)]
    # 'abc' is outside both `when`s, so no strict step sees it
    assert all(r["_sg_errors"] == [] for r in rows)
