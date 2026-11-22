"""Row conservation: every source record ends up loaded or quarantined, exactly once.

    rows_in = loaded + quarantined

The check exists because a mapping can lose rows without producing a single error: a
``filter_rows`` that drops awkward records, a ``skip_rows`` that hides the first ones,
a JSON ``record_path`` that points at a smaller array. Rows that never reach the oracle
can never be quarantined, so the oracle alone cannot notice them.

Two things keep this check out of the mapping's reach:

* ``rows_in`` is counted from the raw file by the profiler (pure Python ``csv``/``json``),
  using its own guesses about delimiter, header and record array. It does not read the
  spec and does not go through DuckDB, so a spec that reads the file differently from
  how the file actually is shows up as a mismatch.
* The comparison is per record id (``_sg_row_id``, the 1-based record number the
  compiler assigns before any filtering), not just per total. Totals alone can be
  balanced by dropping one row and duplicating another; ids cannot.

A batch that fails this check is rejected as a whole: nothing from it is loaded.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from supplygate.profiler import profile_feed


def count_source_records(path: str | Path) -> int:
    """Number of data records in a feed, counted independently of any mapping spec."""
    return profile_feed(path).row_count


@dataclass(frozen=True)
class ConservationResult:
    rows_in: int
    loaded: int
    quarantined: int
    missing: tuple[int, ...]  # source records that came out nowhere
    repeated: tuple[int, ...]  # records that came out more than once (in either or both outputs)
    unknown: tuple[int, ...]  # record ids outside 1..rows_in

    @property
    def ok(self) -> bool:
        return not (self.missing or self.repeated or self.unknown) and self.rows_in == self.loaded + self.quarantined

    def summary(self) -> str:
        head = f"rows_in={self.rows_in} loaded={self.loaded} quarantined={self.quarantined}"
        if self.ok:
            return head + " (conserved)"
        parts = [head]
        for label, ids in (("missing", self.missing), ("repeated", self.repeated), ("unknown", self.unknown)):
            if ids:
                shown = ", ".join(map(str, ids[:10])) + (", ..." if len(ids) > 10 else "")
                parts.append(f"{label} {len(ids)}: [{shown}]")
        return "; ".join(parts)


class ConservationError(RuntimeError):
    """A batch lost, duplicated or invented source records. Nothing from it was loaded."""

    def __init__(self, result: ConservationResult):
        self.result = result
        super().__init__(f"row conservation failed: {result.summary()}")


def check_conservation(rows_in: int, loaded_ids: Iterable[int], quarantined_ids: Iterable[int]) -> ConservationResult:
    """Compare the record ids that came out of a batch against the records that went in."""
    loaded, quarantined = list(loaded_ids), list(quarantined_ids)
    seen = Counter(loaded + quarantined)
    expected = range(1, rows_in + 1)
    return ConservationResult(
        rows_in=rows_in,
        loaded=len(loaded),
        quarantined=len(quarantined),
        missing=tuple(i for i in expected if i not in seen),
        repeated=tuple(sorted(i for i, n in seen.items() if n > 1)),
        unknown=tuple(sorted(i for i in seen if not 1 <= i <= rows_in)),
    )
