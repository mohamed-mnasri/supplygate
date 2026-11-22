"""Feed profiler: describe a supplier file without knowing anything about the supplier.

The profile feeds three consumers:

* the spec's ``source`` block (``FeedProfile.source_options()``): encoding, delimiter, ...
* the matcher and the LLM prompt (roadmap part 4): per-column type guess, samples,
  number format and unit hints
* humans reviewing a new supplier

The profiler reads only the feed file. It is pure Python (no DuckDB) so its guesses are
independent of how the compiler later reads the file. Each guess is a heuristic and is
reported with the evidence it rests on (``FeedProfile.notes``).
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_ROWS = 5000
N_SAMPLES = 5

ValueKind = Literal["empty", "integer", "decimal", "number_with_unit", "date", "boolean", "gtin", "text"]

# --------------------------------------------------------------------------- value classifiers

_INT_RE = re.compile(r"^[+-]?\d+$")
# 1234.5 | 1,234.56 | 1.234,56 | 1234,5 | 1 234,56 | 1'234.50
_NUM_RE = re.compile(r"^[+-]?(\d{1,3}([.,'   ]\d{3})+|\d+)([.,]\d+)?$")
_UNIT_RE = re.compile(r"^(?P<pre>[€$£]\s?)?(?P<num>[+-]?\d[\d.,' \u00a0\u202f]*?)\s?(?P<unit>[A-Za-z€$£%/\u00b5]{1,6}(?:/s)?)?$")
_DATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "%Y-%m-%d"),
    (re.compile(r"^\d{2}\.\d{2}\.\d{4}$"), "%d.%m.%Y"),
    (re.compile(r"^\d{2}/\d{2}/\d{4}$"), "%d/%m/%Y"),  # ambiguous with %m/%d/%Y; see notes
    (re.compile(r"^\d{8}$"), "%Y%m%d"),
]
# values suppliers use for "missing"; reported as hints, they are not removed from the data
NULL_TOKENS = {"n/a", "na", "n.a.", "-", "--", "null", "none", "nil", "?", "k.a.", "nc", "n.c."}
_BOOL_TOKENS = {"true", "false", "yes", "no", "ja", "nein", "oui", "non", "y", "n", "j", "0", "1"}


def classify_value(raw: str) -> ValueKind:
    v = raw.strip()
    if not v:
        return "empty"
    if v.isdigit() and len(v) in (12, 13, 14):
        return "gtin"
    if v.lower() in _BOOL_TOKENS and not v.isdigit():
        return "boolean"
    if _INT_RE.match(v):
        return "integer"
    for pat, _fmt in _DATE_PATTERNS[:3]:
        if pat.match(v):
            return "date"
    if _NUM_RE.match(v):
        return "decimal"
    m = _UNIT_RE.match(v)
    if m and (m.group("pre") or m.group("unit")):
        return "number_with_unit"
    return "text"


def guess_number_format(values: list[str]) -> tuple[str | None, str | None]:
    """Guess (decimal separator, thousands separator) from formatted numbers.

    Rules: a separator followed by exactly 3 digits and then another separator, or appearing
    more than once, is a thousands separator; the last separator followed by 1, 2 or 4+
    digits is the decimal separator. Returns (None, None) without evidence.
    """
    dec: Counter[str] = Counter()
    thou: Counter[str] = Counter()
    for raw in values:
        m = _UNIT_RE.match(raw.strip())
        s = m.group("num").strip() if m else raw.strip()
        s = s.lstrip("+-")
        seps = [(i, ch) for i, ch in enumerate(s) if ch in ".,'   "]
        if not seps:
            continue
        chars = [ch for _, ch in seps]
        last_i, last_ch = seps[-1]
        tail = len(s) - last_i - 1
        if len(set(chars)) == 2:
            dec[last_ch] += 1
            thou[chars[0]] += 1
        elif chars.count(last_ch) > 1:
            thou[last_ch] += 1
        elif tail != 3:
            dec[last_ch] += 1
        elif last_ch in " '  ":
            thou[last_ch] += 1
        # a single '.' or ',' followed by exactly 3 digits is ambiguous: no vote
    d = dec.most_common(1)[0][0] if dec else None
    t = thou.most_common(1)[0][0] if thou else None
    if t in (" ", " "):
        t = " "
    if d is not None and t == d:
        t = None
    if d is None and t in (".", ","):
        d = "," if t == "." else "."
    return d, t


# --------------------------------------------------------------------------- models


class ColumnProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    position: int
    inferred_kind: ValueKind
    kind_counts: dict[str, int]
    null_rate: float
    distinct_count: int
    samples: list[str]
    top_values: list[tuple[str, int]] = Field(default_factory=list)
    min_length: int | None = None
    max_length: int | None = None
    decimal_separator: str | None = None
    thousands_separator: str | None = None
    unit_hints: list[str] = Field(default_factory=list)
    date_format: str | None = None
    null_tokens: list[str] = Field(default_factory=list)  # e.g. ["N/A"]: candidates for null_if


class FeedProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    format: Literal["csv", "json"]
    encoding: str
    bom: bool = False
    delimiter: str | None = None
    quote: str | None = None
    has_header: bool | None = None
    record_path: str | None = None
    row_count: int
    rows_profiled: int
    columns: list[ColumnProfile]
    decimal_separator: str | None = None
    notes: list[str] = Field(default_factory=list)

    def column(self, name: str) -> ColumnProfile:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    def source_options(self) -> dict[str, Any]:
        """A ``source:`` block for a MappingSpec, built from the guesses."""
        opts: dict[str, Any] = {"format": self.format, "encoding": self.encoding}
        if self.format == "csv":
            opts["delimiter"] = self.delimiter or ","
            if self.quote and self.quote != '"':
                opts["quote"] = self.quote
            if self.has_header is False:
                opts["header"] = False
        elif self.record_path:
            opts["record_path"] = self.record_path
        return opts


# --------------------------------------------------------------------------- encoding


def detect_encoding(data: bytes) -> tuple[str, bool, str]:
    """Return (encoding, had_bom, evidence). Only encodings DuckDB can read are returned."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8", True, "UTF-8 byte order mark"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16", True, "UTF-16 byte order mark"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # cp1252 puts printable characters (€, ’, “) at 0x80-0x9f where latin-1 has controls
        if any(0x80 <= b <= 0x9F for b in data):
            return (
                "latin-1",
                False,
                (
                    f"not valid UTF-8 (byte {data[exc.start]:#x} at {exc.start}); bytes 0x80-0x9f present, "
                    "probably Windows-1252, read as latin-1 so those few characters may be wrong"
                ),
            )
        return "latin-1", False, f"not valid UTF-8 (byte {data[exc.start]:#x} at {exc.start}); assuming latin-1"
    has_high = any(b >= 0x80 for b in data)
    return "utf-8", False, "valid UTF-8" + ("" if has_high else " (pure ASCII)")


# --------------------------------------------------------------------------- csv / json readers

_DELIMS = [";", ",", "\t", "|"]


def _sniff_delimiter(text: str) -> tuple[str, str]:
    lines = [ln for ln in text.splitlines()[:50] if ln.strip()]
    if not lines:
        return ",", "empty file, default ','"
    best, best_score, evidence = ",", -1.0, ""
    for d in _DELIMS:
        try:
            rows = list(csv.reader(lines, delimiter=d))
        except csv.Error:
            continue
        widths = [len(r) for r in rows]
        if not widths or max(widths) < 2:
            continue
        mode, freq = Counter(widths).most_common(1)[0]
        # consistent width across lines matters most, then more columns
        score = freq / len(widths) * 100 + min(mode, 50)
        if score > best_score:
            best, best_score = d, score
            evidence = f"{freq}/{len(widths)} lines split into {mode} fields with {d!r}"
    return best, evidence or "no delimiter gives 2+ fields; default ','"


def _looks_like_header(rows: list[list[str]]) -> bool:
    if len(rows) < 2:
        return True
    head, body = rows[0], rows[1:21]
    numeric_header = sum(classify_value(h) in ("integer", "decimal") for h in head)
    if numeric_header:
        return False
    # header if some column is non-text in the body but text in the header
    for i, h in enumerate(head):
        kinds = {classify_value(r[i]) for r in body if i < len(r)}
        if kinds and "text" not in kinds and classify_value(h) == "text":
            return True
    return len(set(head)) == len(head)


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten(v, key))
            else:
                out[key] = v
    else:
        out[prefix or "value"] = obj
    return out


def _find_records(doc: Any) -> tuple[list[Any], str | None]:
    """Locate the main array of records: top-level list, or the largest list of objects."""
    if isinstance(doc, list):
        return doc, None
    best: tuple[list[Any], str | None] = ([], None)

    def walk(node: Any, path: str) -> None:
        nonlocal best
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list) and node and all(isinstance(x, dict) for x in node) and len(node) > len(best[0]):
            best = (node, path)

    walk(doc, "")
    return best


def _to_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


# --------------------------------------------------------------------------- column profiling


def _profile_column(name: str, position: int, values: list[str | None]) -> ColumnProfile:
    n = len(values)
    present = [v for v in values if v is not None and v.strip() != ""]
    tokens = Counter(v.strip() for v in present if v.strip().lower() in NULL_TOKENS)
    typed = [v for v in present if v.strip().lower() not in NULL_TOKENS] if tokens else present
    kinds = Counter(classify_value(v) for v in typed)
    kinds_all = dict(kinds)
    if n - len(present):
        kinds_all["empty"] = n - len(present)
    if tokens:
        kinds_all["null_token"] = sum(tokens.values())
    if not typed:
        inferred: ValueKind = "empty"
    else:
        top, cnt = kinds.most_common(1)[0]
        inferred = top  # type: ignore[assignment]
        # integers mixed with decimals are decimals
        if set(kinds) <= {"integer", "decimal"} and kinds.get("decimal"):
            inferred = "decimal"
        elif cnt / len(typed) < 0.8:
            inferred = "text"
    counts = Counter(present)
    seen: list[str] = []
    for v in present:
        if v not in seen:
            seen.append(v)
        if len(seen) == N_SAMPLES:
            break
    dec = thou = None
    numeric = [v for v in typed if classify_value(v) in ("decimal", "number_with_unit")]
    if numeric and inferred != "text" or len(numeric) >= 0.5 * max(len(typed), 1):
        dec, thou = guess_number_format(numeric)
    units: Counter[str] = Counter()
    for v in typed:
        m = _UNIT_RE.match(v.strip())
        if m and (m.group("pre") or m.group("unit")):
            units[(m.group("pre") or "").strip() or m.group("unit")] += 1
    date_fmt = None
    if inferred == "date":
        fmts = Counter(fmt for v in present for pat, fmt in _DATE_PATTERNS if pat.match(v.strip()))
        date_fmt = fmts.most_common(1)[0][0] if fmts else None
    lengths = [len(v) for v in present]
    return ColumnProfile(
        name=name,
        position=position,
        inferred_kind=inferred,
        kind_counts=kinds_all,
        null_rate=round((n - len(present)) / n, 4) if n else 0.0,
        distinct_count=len(counts),
        samples=seen,
        top_values=counts.most_common(5) if len(counts) <= max(20, n // 10) else [],
        min_length=min(lengths) if lengths else None,
        max_length=max(lengths) if lengths else None,
        decimal_separator=dec,
        thousands_separator=thou,
        unit_hints=[u for u, _ in units.most_common(3)],
        date_format=date_fmt,
        null_tokens=sorted(tokens),
    )


# --------------------------------------------------------------------------- entry point


def profile_feed(path: str | Path, *, max_rows: int = MAX_ROWS) -> FeedProfile:
    """Profile a CSV or JSON supplier feed."""
    p = Path(path)
    data = p.read_bytes()
    encoding, bom, enc_note = detect_encoding(data)
    pyenc = {"utf-8": "utf-8-sig" if bom else "utf-8", "utf-16": "utf-16", "latin-1": "cp1252"}[encoding]
    text = data.decode(pyenc, errors="replace")
    notes = [f"encoding: {enc_note}"]

    stripped = text.lstrip()
    if p.suffix.lower() in (".json", ".jsonl", ".ndjson") or stripped[:1] in ("{", "["):
        return _profile_json(p, text, encoding, bom, notes, max_rows)
    return _profile_csv(p, text, encoding, bom, notes, max_rows)


def _profile_csv(p: Path, text: str, encoding: str, bom: bool, notes: list[str], max_rows: int) -> FeedProfile:
    delim, evidence = _sniff_delimiter(text)
    notes.append(f"delimiter: {evidence}")
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    rows = [r for r in rows if any(c.strip() for c in r)]
    has_header = _looks_like_header(rows)
    if has_header and rows:
        header, body = rows[0], rows[1:]
    else:
        width = max((len(r) for r in rows), default=0)
        header, body = [f"column{i}" for i in range(width)], rows
        notes.append("no header row detected; columns named column0..N")
    ragged = sum(1 for r in body if len(r) != len(header))
    if ragged:
        notes.append(f"{ragged} rows have a different field count than the header")
    sample = body[:max_rows]
    columns = [_profile_column(name, i, [r[i] if i < len(r) else None for r in sample]) for i, name in enumerate(header)]
    return FeedProfile(
        path=str(p),
        format="csv",
        encoding=encoding,
        bom=bom,
        delimiter=delim,
        quote='"',
        has_header=has_header,
        row_count=len(body),
        rows_profiled=len(sample),
        columns=columns,
        decimal_separator=_feed_decimal(columns, notes),
        notes=notes,
    )


def _profile_json(p: Path, text: str, encoding: str, bom: bool, notes: list[str], max_rows: int) -> FeedProfile:
    try:
        doc: Any = json.loads(text)
        records, record_path = _find_records(doc)
        notes.append("json: top-level array" if record_path is None else f"json: records at {record_path!r}")
    except json.JSONDecodeError:
        records = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        record_path = None
        notes.append("json: newline-delimited records")
    flat = [_flatten(r) for r in records[:max_rows]]
    names: list[str] = []
    for f in flat:
        for k in f:
            if k not in names:
                names.append(k)
    columns = [_profile_column(name, i, [_to_text(f.get(name)) for f in flat]) for i, name in enumerate(names)]
    return FeedProfile(
        path=str(p),
        format="json",
        encoding=encoding,
        bom=bom,
        record_path=record_path,
        row_count=len(records),
        rows_profiled=len(flat),
        columns=columns,
        decimal_separator=_feed_decimal(columns, notes),
        notes=notes,
    )


def _feed_decimal(columns: list[ColumnProfile], notes: list[str]) -> str | None:
    votes = Counter(c.decimal_separator for c in columns if c.decimal_separator)
    if not votes:
        return None
    if len(votes) > 1:
        notes.append(f"columns disagree on the decimal separator: {dict(votes)}")
    return votes.most_common(1)[0][0]
