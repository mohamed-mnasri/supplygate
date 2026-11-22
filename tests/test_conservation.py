"""Row conservation: rows_in = loaded + quarantined, checked per record id."""

import pytest

from supplygate.gates.conservation import ConservationError, check_conservation, count_source_records


def test_every_record_accounted_for_once_is_ok():
    r = check_conservation(5, [1, 2, 4], [3, 5])
    assert r.ok and (r.loaded, r.quarantined) == (3, 2)
    assert r.summary().endswith("(conserved)")


def test_dropped_rows_are_reported_as_missing():
    r = check_conservation(5, [1, 2], [5])
    assert not r.ok and r.missing == (3, 4)


def test_balanced_totals_do_not_hide_a_swap():
    # drop record 3, emit record 2 twice: the totals still add up to 5
    r = check_conservation(5, [1, 2, 2, 4], [5])
    assert r.loaded + r.quarantined == r.rows_in
    assert not r.ok and r.missing == (3,) and r.repeated == (2,)


def test_record_in_both_outputs_is_repeated():
    r = check_conservation(2, [1, 2], [2])
    assert not r.ok and r.repeated == (2,)


def test_invented_records_are_unknown():
    # e.g. a header row read as data shifts every id by one and adds one at the end
    r = check_conservation(3, [1, 2, 3, 4], [])
    assert not r.ok and r.unknown == (4,)


def test_error_carries_the_result():
    r = check_conservation(3, [1], [])
    with pytest.raises(ConservationError, match="missing 2"):
        raise ConservationError(r)


def test_long_id_lists_are_shortened_in_the_summary():
    r = check_conservation(100, [], [])
    assert "missing 100: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, ...]" in r.summary()


def test_source_count_ignores_header_and_blank_lines(tmp_path):
    feed = tmp_path / "feed.csv"
    feed.write_text("sku;price\nA;1,00\n\nB;2,00\nC;3,00\n", encoding="utf-8")
    assert count_source_records(feed) == 3


def test_source_count_handles_quoted_newlines(tmp_path):
    feed = tmp_path / "feed.csv"
    feed.write_text('sku,name\nA,"two\nlines"\nB,plain\n', encoding="utf-8")
    assert count_source_records(feed) == 2


def test_source_count_finds_nested_json_records(tmp_path):
    feed = tmp_path / "feed.json"
    feed.write_text('{"meta": {"n": 2}, "data": {"items": [{"sku": "A"}, {"sku": "B"}]}}', encoding="utf-8")
    assert count_source_records(feed) == 2
