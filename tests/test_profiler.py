from pathlib import Path

import pytest

from supplygate.mapping.spec import MappingSpec
from supplygate.profiler import classify_value, detect_encoding, guess_number_format, profile_feed

FEEDS = Path(__file__).parent / "fixtures" / "feeds"


def test_german_csv():
    p = profile_feed(FEEDS / "de_sample.csv")
    assert (p.format, p.encoding, p.delimiter, p.has_header) == ("csv", "utf-8", ";", True)
    assert p.row_count == 3
    assert p.decimal_separator == ","
    price = p.column("Preis")
    assert (price.inferred_kind, price.decimal_separator, price.thousands_separator) == ("decimal", ",", ".")
    assert p.column("Takt").unit_hints == ["GHz"]
    assert p.column("EAN").inferred_kind == "gtin"
    assert p.column("Lager").inferred_kind == "integer"
    speicher = p.column("Speicher")
    assert speicher.null_rate == pytest.approx(2 / 3, abs=1e-3) and speicher.unit_hints == ["GB"]


def test_french_latin1_csv():
    p = profile_feed(FEEDS / "fr_sample.csv")
    assert p.encoding == "latin-1"
    assert [c.name for c in p.columns][:3] == ["Référence", "Marque", "Désignation"]
    assert p.column("Mémoire vidéo").unit_hints == ["Go"]
    assert p.column("Disponibilité").inferred_kind == "boolean"
    prix = p.column("Prix TTC")
    assert (prix.inferred_kind, prix.decimal_separator, prix.null_tokens) == ("decimal", ",", ["N/A"])
    assert any("UTF-8" in n for n in p.notes)


def test_uk_csv_currency_prefix():
    p = profile_feed(FEEDS / "uk_sample.csv")
    assert p.delimiter == ","
    price = p.column("price_gbp")
    assert price.unit_hints == ["£"]
    assert (price.decimal_separator, price.thousands_separator) == (".", ",")
    assert p.column("speed").unit_hints == ["MHz"]


def test_nested_json():
    p = profile_feed(FEEDS / "json_sample.json")
    assert p.format == "json" and p.record_path == "data.items" and p.row_count == 2
    names = [c.name for c in p.columns]
    assert names == ["id", "brand", "title", "pricing.amount_cents", "specs.socket", "specs.cores"]
    assert p.column("pricing.amount_cents").inferred_kind == "integer"


def test_ndjson_and_top_level_array(tmp_path):
    nd = tmp_path / "f.jsonl"
    nd.write_text('{"a": 1}\n{"a": 2, "b": {"c": "x"}}\n')
    p = profile_feed(nd)
    assert p.record_path is None and [c.name for c in p.columns] == ["a", "b.c"]
    assert p.column("b.c").null_rate == 0.5


def test_source_options_validate_as_a_spec():
    for feed in ("de_sample.csv", "fr_sample.csv", "uk_sample.csv", "json_sample.json"):
        p = profile_feed(FEEDS / feed)
        first = p.columns[0].name
        spec = MappingSpec.model_validate({"supplier": "x", "source": p.source_options(), "columns": [{"target": "a", "source": first}]})
        assert spec.source.format == p.format


def test_profile_drives_a_working_mapping():
    """Profile -> source options -> spec -> DuckDB reads the file as the profiler saw it."""
    import duckdb

    from supplygate.mapping import run_mapping

    p = profile_feed(FEEDS / "fr_sample.csv")
    spec = MappingSpec.model_validate(
        {"supplier": "fr", "source": p.source_options(), "columns": [{"target": "a", "source": p.columns[0].name}]}
    )
    rows = run_mapping(duckdb.connect(), spec, FEEDS / "fr_sample.csv", {"a": "VARCHAR"}).fetchall()
    assert len(rows) == p.row_count


def test_headerless_csv(tmp_path):
    f = tmp_path / "h.csv"
    f.write_text("1;2,5;x\n2;3,5;y\n3;4,0;z\n")
    p = profile_feed(f)
    assert p.has_header is False and p.source_options()["header"] is False
    assert [c.name for c in p.columns] == ["column0", "column1", "column2"]


def test_bom_and_tab_delimiter(tmp_path):
    f = tmp_path / "t.tsv"
    f.write_bytes("﻿a\tb\n1\tx\n2\ty\n".encode())
    p = profile_feed(f)
    assert p.bom and p.encoding == "utf-8" and p.delimiter == "\t"
    assert p.columns[0].name == "a"


def test_detect_encoding():
    assert detect_encoding("é".encode())[0] == "utf-8"
    assert detect_encoding("é".encode("latin-1"))[0] == "latin-1"
    enc, _, note = detect_encoding("€5".encode("cp1252"))
    assert enc == "latin-1" and "1252" in note
    assert detect_encoding("ab".encode("utf-16"))[:2] == ("utf-16", True)


@pytest.mark.parametrize(
    "values, expected",
    [
        (["1.299,50", "329,00"], (",", ".")),
        (["1,299.50", "54.50"], (".", ",")),
        (["4,2 GHz", "3,5 GHz"], (",", None)),
        (["1 299,50"], (",", " ")),
        (["1.299.000"], (",", ".")),
        (["1.299"], (None, None)),  # ambiguous on its own
        (["12", "13"], (None, None)),
    ],
)
def test_guess_number_format(values, expected):
    assert guess_number_format(values) == expected


@pytest.mark.parametrize(
    "value, kind",
    [
        ("", "empty"),
        ("2001234567893", "gtin"),
        ("12", "integer"),
        ("1.299,50", "decimal"),
        ("16 GB", "number_with_unit"),
        ("£54.50", "number_with_unit"),
        ("3200MHz", "number_with_unit"),
        ("24.12.2025", "date"),
        ("2025-12-24", "date"),
        ("ja", "boolean"),
        ("Ryzen 7 7800X3D", "text"),
    ],
)
def test_classify_value(value, kind):
    assert classify_value(value) == kind
