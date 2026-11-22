"""Load one supplier feed into the canonical catalog, through the gates.

One call to :func:`load_feed` is one *run*: one feed file mapped with one spec.

1. Count the records in the raw file, without the spec (``gates.conservation``).
2. Map the feed with the compiled spec into a temporary table.
3. Run the plausibility oracle over the mapped rows (``gates.oracle``).
4. Inside one transaction:
   a. replace the supplier's previous offers with the rows that passed,
   b. write every other row to ``quarantine`` with the reasons it failed,
   c. read back what was actually written and check row conservation on it.
5. If conservation holds, record the run and commit. If not, roll back, record the run
   as ``rejected`` and raise :class:`ConservationError`. The catalog is never left with
   half a batch.

A row is quarantined when the mapping reported a parse error for it (``_sg_errors``,
e.g. a price that did not parse) or when it breaks an oracle rule. Both lists are kept
separately in the quarantine table so a reviewer can tell "the spec could not read this
value" apart from "the value was read but is not plausible".

Lineage tables answer "where did this value come from?":

* ``load_runs``: one row per run, with the SHA-256 of the feed file and of the spec, the
  spec itself, the risky operations it used and the conservation counts.
* ``offer_lineage``: for each offer currently in the catalog, the run and the source
  record (1-based row number in the feed) it was loaded from.
* ``column_lineage``: per run, which source columns and transforms produced each
  canonical column.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from supplygate.gates.conservation import ConservationError, ConservationResult, check_conservation, count_source_records
from supplygate.gates.oracle import DEFAULT_CONFIG, VIOLATIONS_COLUMN, OracleConfig, build_rules, oracle_sql
from supplygate.mapping.compiler import MissingSourceColumns, compile_spec, feed_columns
from supplygate.mapping.spec import MappingSpec
from supplygate.schema import TABLE_NAME, canonical_columns, create_tables

AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS load_runs (
    run_id            VARCHAR PRIMARY KEY,
    supplier_id       VARCHAR NOT NULL,
    status            VARCHAR NOT NULL CHECK (status IN ('committed', 'rejected')),
    started_at        TIMESTAMP NOT NULL,
    source_path       VARCHAR NOT NULL,
    source_sha256     VARCHAR NOT NULL,
    spec_sha256       VARCHAR NOT NULL,
    spec_yaml         VARCHAR NOT NULL,
    risky_ops         VARCHAR[] NOT NULL,
    rows_in           INTEGER NOT NULL,
    rows_loaded       INTEGER NOT NULL,
    rows_quarantined  INTEGER NOT NULL,
    conservation      VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    run_id        VARCHAR NOT NULL,
    supplier_id   VARCHAR NOT NULL,
    source_row    INTEGER NOT NULL,
    supplier_sku  VARCHAR,
    parse_errors  VARCHAR[] NOT NULL,
    violations    VARCHAR[] NOT NULL,
    record        JSON NOT NULL,
    PRIMARY KEY (run_id, source_row)
);
CREATE TABLE IF NOT EXISTS offer_lineage (
    supplier_id   VARCHAR NOT NULL,
    supplier_sku  VARCHAR NOT NULL,
    run_id        VARCHAR NOT NULL,
    source_row    INTEGER NOT NULL,
    PRIMARY KEY (supplier_id, supplier_sku)
);
CREATE TABLE IF NOT EXISTS column_lineage (
    run_id          VARCHAR NOT NULL,
    target_column   VARCHAR NOT NULL,
    source_columns  VARCHAR[] NOT NULL,
    constant        VARCHAR,
    transforms      VARCHAR[] NOT NULL,
    PRIMARY KEY (run_id, target_column)
);
"""

# Scratch tables for one run. TEMP tables live only on this connection.
_MAPPED = "_sg_mapped"
_CHECKED = "_sg_checked"

# A row passes when the mapping reported no parse error and the oracle found no violation.
_PASSED = f"(len(_sg_errors) = 0 AND len({VIOLATIONS_COLUMN}) = 0)"


def create_audit_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(AUDIT_DDL)


@dataclass(frozen=True)
class LoadReport:
    """Outcome of a committed run. A rejected run raises ConservationError instead."""

    run_id: str
    supplier_id: str
    conservation: ConservationResult
    reasons: Counter[str]  # quarantine reason -> number of rows that have it
    risky_ops: tuple[str, ...]

    @property
    def rows_in(self) -> int:
        return self.conservation.rows_in

    @property
    def loaded(self) -> int:
        return self.conservation.loaded

    @property
    def quarantined(self) -> int:
        return self.conservation.quarantined


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _map_into_temp_table(con: duckdb.DuckDBPyConnection, spec: MappingSpec, feed: Path) -> None:
    compiled = compile_spec(spec, feed, canonical_columns())
    if spec.source.format == "csv":
        available = feed_columns(con, spec, feed)
        missing = sorted(spec.source_columns() - set(available))
        if missing:
            raise MissingSourceColumns(missing, available)
    con.execute(f"CREATE OR REPLACE TEMP TABLE {_MAPPED} AS {compiled.sql}")


def _write_batch(con: duckdb.DuckDBPyConnection, run_id: str, supplier_id: str) -> None:
    """Steps 4a and 4b. Runs inside the caller's transaction."""
    cols = list(canonical_columns())
    col_list = ", ".join(cols)

    con.execute(f"DELETE FROM {TABLE_NAME} WHERE supplier_id = ?", [supplier_id])
    con.execute("DELETE FROM offer_lineage WHERE supplier_id = ?", [supplier_id])

    con.execute(f"INSERT INTO {TABLE_NAME} ({col_list}) SELECT {col_list} FROM {_CHECKED} WHERE {_PASSED}")
    con.execute(
        f"INSERT INTO offer_lineage SELECT supplier_id, supplier_sku, ?, _sg_row_id FROM {_CHECKED} WHERE {_PASSED}",
        [run_id],
    )
    # the full mapped record is kept as JSON so a reviewer sees what the mapping produced
    record = "to_json(struct_pack(" + ", ".join(f"{c} := {c}" for c in cols) + "))"
    con.execute(
        f"INSERT INTO quarantine SELECT ?, ?, _sg_row_id, supplier_sku, _sg_errors, {VIOLATIONS_COLUMN}, {record} "
        f"FROM {_CHECKED} WHERE NOT {_PASSED}",
        [run_id, supplier_id],
    )


def _written_ids(con: duckdb.DuckDBPyConnection, run_id: str, supplier_id: str) -> tuple[list[int], list[int]]:
    """Source record ids as they actually landed, read back from the output tables.

    Joining offer_lineage to the catalog makes sure a lineage row only counts if its
    offer really exists.
    """
    loaded = con.execute(
        f"SELECT l.source_row FROM offer_lineage l JOIN {TABLE_NAME} o USING (supplier_id, supplier_sku) "
        "WHERE l.run_id = ? AND l.supplier_id = ?",
        [run_id, supplier_id],
    ).fetchall()
    quarantined = con.execute("SELECT source_row FROM quarantine WHERE run_id = ?", [run_id]).fetchall()
    return [r[0] for r in loaded], [r[0] for r in quarantined]


def _write_column_lineage(con: duckdb.DuckDBPyConnection, run_id: str, spec: MappingSpec) -> None:
    rows = [
        [run_id, c.target, c.source_columns(), None if c.const is None else str(c.const), [t.op for t in c.transforms]]
        for c in spec.columns
    ]
    con.executemany("INSERT INTO column_lineage VALUES (?, ?, ?, ?, ?)", rows)


@dataclass(frozen=True)
class _Run:
    """What is known about a run before it starts; written to ``load_runs`` at the end."""

    run_id: str
    supplier_id: str
    started_at: datetime
    feed: Path
    feed_sha256: str  # hashed before mapping, so it describes the bytes that were read
    spec_yaml: str
    risky_ops: tuple[str, ...]

    def record(self, con: duckdb.DuckDBPyConnection, status: str, result: ConservationResult) -> None:
        con.execute(
            "INSERT INTO load_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                self.run_id,
                self.supplier_id,
                status,
                self.started_at,
                str(self.feed),
                self.feed_sha256,
                _sha256(self.spec_yaml.encode("utf-8")),
                self.spec_yaml,
                list(self.risky_ops),
                result.rows_in,
                result.loaded,
                result.quarantined,
                result.summary(),
            ],
        )


def load_feed(
    con: duckdb.DuckDBPyConnection,
    spec: MappingSpec,
    feed_path: str | Path,
    *,
    config: OracleConfig = DEFAULT_CONFIG,
) -> LoadReport:
    """Map, check and load one feed for ``spec.supplier``. See the module docstring."""
    feed = Path(feed_path)
    supplier_id = spec.supplier
    run = _Run(
        run_id=uuid.uuid4().hex,
        supplier_id=supplier_id,
        started_at=datetime.now(UTC).replace(tzinfo=None),
        feed=feed,
        feed_sha256=_sha256(feed.read_bytes()),
        spec_yaml=spec.to_yaml(),
        risky_ops=tuple(f"{u.op}@{u.target}" if u.target else u.op for u in spec.risky_ops()),
    )

    create_tables(con)
    create_audit_tables(con)

    rows_in = count_source_records(feed)  # step 1: before the spec touches the file
    try:
        _map_into_temp_table(con, spec, feed)  # step 2
        con.execute(f"CREATE OR REPLACE TEMP TABLE {_CHECKED} AS {oracle_sql(_MAPPED, build_rules(supplier_id, config))}")  # step 3

        reasons: Counter[str] = Counter()
        for errors, violations in con.execute(f"SELECT _sg_errors, {VIOLATIONS_COLUMN} FROM {_CHECKED}").fetchall():
            reasons.update(f"parse:{e}" for e in errors)
            reasons.update(violations)

        con.begin()  # step 4
        open_tx = True
        try:
            _write_batch(con, run.run_id, supplier_id)
            result = check_conservation(rows_in, *_written_ids(con, run.run_id, supplier_id))
            if result.ok:  # step 5
                _write_column_lineage(con, run.run_id, spec)
                run.record(con, "committed", result)
                con.commit()
            else:
                con.rollback()
            open_tx = False
        finally:
            if open_tx:  # an exception escaped the block above
                con.rollback()
        if not result.ok:
            # after the rollback: the rejected run is recorded on its own, nothing else is
            run.record(con, "rejected", result)
    finally:
        con.execute(f"DROP TABLE IF EXISTS {_CHECKED}")
        con.execute(f"DROP TABLE IF EXISTS {_MAPPED}")

    if not result.ok:
        raise ConservationError(result)
    return LoadReport(run.run_id, supplier_id, result, reasons, run.risky_ops)
