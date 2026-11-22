# Gates, quarantine and lineage

Roadmap item 3. Code: `src/supplygate/gates/` (oracle, conservation) and `src/supplygate/loader.py`.

A mapping spec turns a supplier feed into canonical rows. The gates decide which of those rows
may enter `canonical_offers`. They are deliberately kept out of the mapping's reach: they read
the mapped values and the raw file, never the spec, so a spec (hand-written or proposed by an
LLM) cannot switch a check off by how it is written.

```
feed file ──► count records (profiler, no spec) ─────────────────────────┐
    │                                                                     ▼
    └──► mapping (compiled spec) ──► oracle ──► passed ──► canonical_offers ──► conservation ──► commit
                                          └──► failed ──► quarantine ───────┘        │
                                                                                   fails ──► rollback, run "rejected"
```

## Running it

```powershell
.\.venv\Scripts\supplygate.exe generate --seed 42 --out data/generated
.\.venv\Scripts\supplygate.exe load --spec specs/dev/de_csv.yaml --feed data/generated/dev/de_csv/input/feed.csv
```

```python
from supplygate.loader import load_feed
report = load_feed(con, spec, "feed.csv")   # raises ConservationError if records went missing
report.conservation.summary()               # 'rows_in=32 loaded=26 quarantined=6 (conserved)'
report.reasons                              # Counter({'negative_stock': 2, 'price_range': 1, ...})
```

## Plausibility oracle

Each rule is a named SQL predicate that is TRUE when a row is implausible. All rules run in one
DuckDB query; the names of the rules a row breaks are stored with it in quarantine. A rule whose
inputs are NULL does not fire: whether a value may be missing is decided only by `required:*`.
The thresholds live in `OracleConfig` and are deliberately wide. They catch values that are off by
an order of magnitude or a unit, not prices that are merely high.

| Rule | Fires when |
|---|---|
| `required:<column>` | `supplier_id`, `supplier_sku`, `category`, `brand`, `model_name`, `gtin` or `price_eur` is NULL or blank. |
| `supplier_mismatch` | The row's `supplier_id` differs from the supplier the spec declares. |
| `duplicate_key` | An earlier row in the same feed has the same `(supplier_id, supplier_sku)`. The first row wins. |
| `enum:<column>` | A value outside the schema's allowed values (socket, memory type, ...). |
| `foreign_attributes:<category>` | A row carries `cpu_*`, `gpu_*` or `ram_*` values but is not of that category. |
| `gtin_format` | GTIN is not exactly 13 digits. |
| `gtin_checksum` | GTIN check digit is wrong. Misses swaps of adjacent digits that differ by 5 (an EAN-13 property). |
| `price_range` | Price outside CPU 40-1500, GPU 150-3500, RAM 15-1000 EUR. Catches a 100x shift, not a 10% drift. |
| `negative_stock` | `stock_qty < 0`. |
| `cpu_socket_memory` | Memory support the socket's platform does not have (AM4 = DDR4, AM5 and LGA1851 = DDR5). |
| `cpu_threads_vs_cores` | Threads below the core count or above twice the core count. |
| `cpu_clock_order` | Boost clock below base clock. |
| `cpu_clock_range`, `gpu_clock_range` | Clock outside 1000-6500 MHz (CPU) or 1000-3500 MHz (GPU): usually a GHz/MHz slip. |
| `cpu_tdp_range`, `gpu_tdp_range` | Power outside 15-400 W (CPU) or 30-700 W (GPU). |
| `gpu_vram_size` | VRAM not in {4, 6, 8, 10, 12, 16, 20, 24, 32, 48} GB. Also catches MB given as GB. |
| `ram_kit_layout` | Kit capacity, module count or capacity per module is not a real combination. |
| `ram_speed_range` | Data rate outside DDR4 1600-5333 or DDR5 4000-9600 MT/s. |
| `ram_cas_latency_range` | CL outside 9-60. |

`tests/test_oracle.py` checks that every product in the seed catalog passes as a clean offer (so no
rule rejects real hardware) and that each rule fires on its own defect and nothing else. The SQL
GTIN checksum is tested against the Python one in `gtin.py` on 2,000 random values.

## Quarantine

A row is quarantined if the mapping reported a parse error for it (`_sg_errors`, e.g. a price that
did not parse) or it breaks at least one oracle rule. Table `quarantine` keeps the two apart, so
"the spec could not read this value" and "the value was read but is not plausible" stay
distinguishable:

| Column | Content |
|---|---|
| `run_id`, `supplier_id`, `source_row`, `supplier_sku` | Where the row came from. `source_row` is the 1-based record number in the feed. |
| `parse_errors` | From the compiler, e.g. `price_eur:parse_number`. |
| `violations` | Oracle rule names, sorted. |
| `record` | The full mapped row as JSON, for review. |

## Row conservation

`rows_in = loaded + quarantined`, checked per record id, not just per total.

- `rows_in` is counted by the profiler, in pure Python, with its own guesses about delimiter, header
  and record array. It never sees the spec. A spec that reads the file differently from how the
  file is (a `filter_rows` that drops records, `skip_rows`, `header: false`, a JSON `record_path`
  that picks a smaller array) produces a mismatch.
- The loaded and quarantined ids are read back from the tables after writing, inside the
  transaction. So the check is on what actually landed, not on what the loader meant to write.
- Totals alone can be balanced by dropping one record and emitting another twice. Ids cannot:
  the result lists `missing`, `repeated` and `unknown` ids separately.

If the check fails, the transaction is rolled back, the run is recorded in `load_runs` with status
`rejected` and the reason, and `ConservationError` is raised. The CLI exits with code 2.

## Lineage

| Table | One row per | Answers |
|---|---|---|
| `load_runs` | run | Which feed bytes (`source_sha256`) and which spec (`spec_sha256`, `spec_yaml`) produced this load, which risky operations the spec used, the conservation counts, and whether it was committed or rejected. |
| `offer_lineage` | offer in the catalog | Which run and which source record the offer was loaded from. |
| `column_lineage` | run x canonical column | Which source columns, constant and transforms produced the column. |

Loading a supplier again replaces its offers and their lineage (a feed is treated as the
supplier's full current list). Quarantine rows and run records are kept for every run.

## Known limits

- **Plausible but wrong values pass.** A price off by 10% or a boost clock off by 100 MHz is within
  every range. Catching those needs the product matcher (roadmap item 4) to compare an offer against
  the catalog entry it matches.
- **The dev result is not an evaluation.** On the dev feeds the quarantined rows equal the
  ground truth's expected quarantine set exactly (`tests/test_loader.py`), but the rules were
  written with the dev corruption families in view. The honest number comes from the held-out
  suppliers and corruption families, scored in roadmap item 6.
- **`cast_loose` on an optional column is invisible.** If a spec swallows a failed cast on
  `stock_qty`, the value loads as NULL and no rule fires, because stock may be missing. The spec's
  risky operations are listed in `load_runs.risky_ops` for the router (roadmap item 5) to act on.
- **Duplicates: first row wins.** If the first copy of a SKU is the bad one and the second is
  clean, the first is quarantined for its defect and the second for being a duplicate.
