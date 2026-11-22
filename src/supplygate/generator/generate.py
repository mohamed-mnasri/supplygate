"""Generate synthetic supplier feeds, ground truth and corruption logs.

Output layout (``out_dir``)::

    manifest.json
    <split>/<supplier_id>/input/...            # what the pipeline may read
    <split>/<supplier_id>/truth/ground_truth.csv
    <split>/<supplier_id>/truth/corruptions.jsonl

Everything under ``truth/`` is for scoring only. The profiler, mapper and gates
must never read it.

Generation is deterministic for a given seed and config: same inputs, same bytes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from supplygate.catalog import CatalogProduct, load_catalog
from supplygate.generator.corruptions import FAMILIES, Change, Family, RowState, families_for_split
from supplygate.generator.suppliers import SUPPLIERS, Supplier
from supplygate.schema import canonical_columns

GENERATOR_VERSION = "1"
GROUND_TRUTH_PREFIX = ["source_row", "product_id", "expected_outcome", "corruption"]


@dataclass(frozen=True)
class GenerationConfig:
    seed: int = 42
    corruption_rate: float = 0.25  # share of catalog rows that get one corruption
    coverage: float = 0.8  # share of the catalog each supplier lists
    suppliers: tuple[str, ...] = tuple(SUPPLIERS)


@dataclass
class GeneratedRow:
    state: RowState  # values as rendered into the feed (possibly corrupted)
    clean: dict[str, Any]  # canonical ground truth
    family: Family | None = None
    change: Change | None = None

    @property
    def expected_outcome(self) -> str:
        if self.family and self.change and self.change.expected_oracle_visible and not self.family.benign:
            return "quarantine"
        return "load"


@dataclass
class SupplierFeed:
    supplier: Supplier
    rows: list[GeneratedRow]
    files: dict[str, bytes] = field(default_factory=dict)

    def ground_truth_csv(self) -> bytes:
        cols = list(canonical_columns())
        buf = io.StringIO(newline="")
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(GROUND_TRUTH_PREFIX + cols)
        for i, row in enumerate(self.rows, start=1):
            prefix = [i, row.state.product_id, row.expected_outcome, row.family.name if row.family else ""]
            w.writerow(prefix + ["" if row.clean[c] is None else str(row.clean[c]) for c in cols])
        return buf.getvalue().encode("utf-8")

    def corruption_records(self) -> list[dict[str, Any]]:
        records = []
        for i, row in enumerate(self.rows, start=1):
            if row.family is None or row.change is None:
                continue
            records.append({
                "supplier_id": self.supplier.supplier_id,
                "source_row": i,
                "supplier_sku": row.clean["supplier_sku"],
                "product_id": row.state.product_id,
                "family": row.family.name,
                "family_split": row.family.split,
                "column": row.change.column,
                "clean_value": _jsonable(row.change.clean_value),
                "corrupted_value": _jsonable(row.change.corrupted_value),
                "expected_oracle_visible": row.change.expected_oracle_visible,
                "benign": row.family.benign,
                "expected_outcome": row.expected_outcome,
            })  # fmt: skip
        return records

    def corruptions_jsonl(self) -> bytes:
        return "".join(json.dumps(r, sort_keys=True) + "\n" for r in self.corruption_records()).encode("utf-8")


def _jsonable(v: Any) -> Any:
    return str(v) if isinstance(v, Decimal) else v


def _native_price(product: CatalogProduct, supplier: Supplier, rng: random.Random) -> Decimal:
    """Shop-style price in the supplier's currency (x.90 / x.99 endings)."""
    target_eur = product.reference_price_eur * Decimal(str(round(rng.uniform(0.92, 1.12), 4)))
    native = (target_eur / supplier.fx_to_eur).quantize(Decimal(1), ROUND_HALF_UP)
    native -= rng.choice([Decimal("0.01"), Decimal("0.10")])
    if supplier.price_in_cents:
        return (native * 100).quantize(Decimal(1))
    return native


def _stock(rng: random.Random) -> int:
    return 0 if rng.random() < 0.1 else rng.randint(1, 150)


def generate_supplier(supplier: Supplier, config: GenerationConfig, catalog: tuple[CatalogProduct, ...] | None = None) -> SupplierFeed:
    catalog = catalog if catalog is not None else load_catalog()
    rng = random.Random(f"{config.seed}:{supplier.supplier_id}")
    families = [f for f in families_for_split(supplier.split) if f.name not in supplier.excluded_families]
    products = rng.sample(list(catalog), k=round(config.coverage * len(catalog)))

    rows: list[GeneratedRow] = []
    for n, product in enumerate(products):
        native = _native_price(product, supplier, rng)
        offer = product.to_offer(supplier.supplier_id, supplier.sku(n, product.product_id), supplier.price_to_eur(native), _stock(rng))
        clean = offer.model_dump()
        state = RowState(dict(clean), native, supplier.price_to_eur, product.product_id)

        if rng.random() >= config.corruption_rate:
            rows.append(GeneratedRow(state, clean))
            continue
        candidates = [f for f in families if clean["category"] in f.categories and f.applicable(state)]
        family = rng.choice(candidates)
        if family.duplicates_row:
            rows.append(GeneratedRow(state, clean))
            copy = RowState(dict(clean), native, supplier.price_to_eur, product.product_id)
            rows.append(GeneratedRow(copy, clean, family, family.apply(copy, rng)))
            continue
        change = family.apply(state, rng)
        rows.append(GeneratedRow(state, clean, family if change else None, change))

    feed = SupplierFeed(supplier, rows)
    feed.files = supplier.render([r.state for r in rows])
    return feed


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generate(out_dir: str | Path, config: GenerationConfig | None = None) -> dict[str, Any]:
    """Write all feeds, truth files and ``manifest.json`` under ``out_dir``. Returns the manifest."""
    config = config or GenerationConfig()
    out = Path(out_dir)
    catalog = load_catalog()
    manifest: dict[str, Any] = {
        "generator_version": GENERATOR_VERSION,
        "config": {**asdict(config), "suppliers": list(config.suppliers)},
        "catalog_products": len(catalog),
        "families": {f.name: {"split": f.split, "description": f.description, "benign": f.benign} for f in FAMILIES},
        "suppliers": {},
    }
    for supplier_id in config.suppliers:
        supplier = SUPPLIERS[supplier_id]
        feed = generate_supplier(supplier, config, catalog)
        base = out / supplier.split / supplier_id
        files = {f"input/{name}": data for name, data in feed.files.items()}
        files["truth/ground_truth.csv"] = feed.ground_truth_csv()
        files["truth/corruptions.jsonl"] = feed.corruptions_jsonl()
        for rel, data in files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        records = feed.corruption_records()
        manifest["suppliers"][supplier_id] = {
            "split": supplier.split,
            "description": supplier.description,
            "path": f"{supplier.split}/{supplier_id}",
            "format": supplier.format,
            "delimiter": supplier.delimiter,
            "input_files": {f"input/{n}": {"encoding": enc, "sha256": _sha256(feed.files[n])} for n, enc in supplier.files.items()},
            "currency": supplier.currency,
            "fx_to_eur": str(supplier.fx_to_eur),
            "price_in_cents": supplier.price_in_cents,
            "provides": list(supplier.provides),
            "rows": len(feed.rows),
            "corrupted_rows": len(records),
            "expected_quarantine": sum(r.expected_outcome == "quarantine" for r in feed.rows),
            "corruptions_by_family": {f: sum(r["family"] == f for r in records) for f in sorted({r["family"] for r in records})},
        }
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
