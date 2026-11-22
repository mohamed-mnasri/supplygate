# Canonical schema and synthetic supplier data

Roadmap item 1. Code: `src/supplygate/schema.py`, `src/supplygate/catalog.py`, `src/supplygate/generator/`.

## Canonical table `canonical_offers`

One row per supplier offer. One wide table: common columns plus category-specific columns
(`cpu_*`, `gpu_*`, `ram_*`); columns of the other categories must be NULL. Units are fixed and
part of the column name. Primary key: `(supplier_id, supplier_sku)`.

The Pydantic model `CanonicalOffer` is the single source of truth. The DuckDB DDL (`schema.ddl()`)
and the column listing used by the mapping compiler (`schema.canonical_columns(category=None)`)
are derived from it.

| Column | DuckDB type | Unit | Category | Description |
|---|---|---|---|---|
| `supplier_id` | VARCHAR NOT NULL |  | all | Supplier identifier, e.g. 'de_csv'. |
| `supplier_sku` | VARCHAR NOT NULL |  | all | Supplier's own article number. |
| `category` | VARCHAR NOT NULL |  | all | Product category: cpu, gpu or ram. |
| `gtin` | VARCHAR |  | all | GTIN-13 / EAN-13 as 13 digits. Checksum is checked by the oracle, not here. |
| `mpn` | VARCHAR |  | all | Manufacturer part number. |
| `brand` | VARCHAR NOT NULL |  | all | Manufacturer / brand name, e.g. 'AMD'. |
| `model_name` | VARCHAR NOT NULL |  | all | Product model name without brand, e.g. 'Ryzen 7 7800X3D'. |
| `price_eur` | DECIMAL(10,2) | EUR | all | Offer price in EUR incl. VAT, 2 decimals. |
| `stock_qty` | INTEGER |  | all | Units available at the supplier. |
| `cpu_socket` | VARCHAR |  | cpu | CPU socket. |
| `cpu_cores` | INTEGER |  | cpu | Physical cores (P+E). |
| `cpu_threads` | INTEGER |  | cpu | Hardware threads. |
| `cpu_base_clock_mhz` | INTEGER | MHz | cpu | Base clock (P-core base for hybrid CPUs). |
| `cpu_boost_clock_mhz` | INTEGER | MHz | cpu | Max boost clock. |
| `cpu_tdp_w` | INTEGER | W | cpu | TDP / base power. |
| `cpu_memory_support` | VARCHAR |  | cpu | Supported memory generation(s). |
| `gpu_chipset` | VARCHAR |  | gpu | GPU chip, e.g. 'GeForce RTX 4070'. |
| `gpu_vram_gb` | INTEGER | GB | gpu | Video memory. |
| `gpu_vram_type` | VARCHAR |  | gpu | Video memory type. |
| `gpu_boost_clock_mhz` | INTEGER | MHz | gpu | Boost clock. |
| `gpu_tdp_w` | INTEGER | W | gpu | Total board power. |
| `ram_type` | VARCHAR |  | ram | Memory generation. |
| `ram_capacity_gb` | INTEGER | GB | ram | Total kit capacity. |
| `ram_modules` | INTEGER |  | ram | Number of modules in the kit. |
| `ram_speed_mts` | INTEGER | MT/s | ram | Data rate. |
| `ram_cas_latency` | INTEGER |  | ram | CAS latency (CL). |

Allowed values:

- `category`: `cpu`, `gpu`, `ram`
- `cpu_socket`: `AM4`, `AM5`, `LGA1700`, `LGA1851`
- `cpu_memory_support`: `DDR4`, `DDR5`, `DDR4/DDR5`
- `gpu_vram_type`: `GDDR6`, `GDDR6X`, `GDDR7`
- `ram_type`: `DDR4`, `DDR5`

**What the schema checks:** types, enums, 13-digit GTIN shape, 2-decimal prices, no attributes of
another category. **What it does not check:** GTIN checksums, plausible ranges, socket/memory
compatibility, duplicates. Those are the oracle's job ([gates.md](gates.md)), kept separate so the mapping
step cannot weaken them.

## Seed catalog

`src/supplygate/seed/catalog.yaml`: 39 real products (15 CPUs, 14 GPUs, 10 RAM kits).
GTINs are synthetic: prefix `200` (GS1 restricted circulation) plus a hash of `product_id`, with a valid
check digit, so they can never collide with a real barcode. Specs still need checking against
manufacturer pages before publishing; see [seed_catalog.md](seed_catalog.md).

## Suppliers

Each supplier lists about 80% of the catalog in its own format. Format differences are the mapping
problem, not corruptions. For every column in a supplier's `provides` list (in `manifest.json`), the
canonical value can be recovered exactly from the feed; `tests/feed_decoders.py` proves this per supplier.

| Supplier | Split | Input files | Price | Format notes |
|---|---|---|---|---|
| `de_csv` | dev | `feed.csv` (utf-8-sig) | EUR | German shop export: ';' separator, decimal comma, German headers, clocks in GHz, units in cells. |
| `fr_csv` | dev | `feed.csv` (latin-1) | EUR | French distributor: Latin-1, French headers, decimal comma with no-break-space thousands, 'Go' units. |
| `uk_csv` | dev | `feed.csv` (utf-8) | GBP | UK wholesaler: GBP prices with '£', clocks in MHz, memory clock in MHz (half the MT/s rate). |
| `spec_txt` | heldout | `feed.csv` (utf-8) | EUR | Specs only in a free-text SPEC_TXT column; no MPN. |
| `legacy_erp` | heldout | `feed.txt` (cp1252), `code_tables.csv` (cp1252) | EUR cents | Legacy ERP: pipe-separated cp1252, cryptic headers, prices in cents, code tables, positional A1-A7 attributes per category, VRAM in MB, names truncated to 30 upper-case characters. |
| `json_api` | heldout | `feed.json` (utf-8) | EUR | Nested JSON API: identifiers/offer objects, specs as name/value/unit lists, GHz clocks. |

Not provided: `spec_txt` has no MPN; `legacy_erp` truncates names to 30 upper-case characters, so
`model_name` is not scored for it. `uk_csv` prices convert with a fixed rate of 1.17 EUR/GBP,
`price_eur = round(gbp * 1.17, 2)`.

## Corruption families

A share of rows (default 25%) gets exactly one corruption, logged in `truth/corruptions.jsonl`.
Dev feeds get only dev families. Held-out feeds get dev and held-out families, so held-out families
can be scored separately and never seen while developing the oracle.

| Family | Split | Categories | Oracle should catch? | What changes |
|---|---|---|---|---|
| `gtin_bad_checksum` | dev | cpu, gpu, ram | yes | Wrong GTIN check digit. |
| `missing_required` | dev | cpu, gpu, ram | yes | GTIN or price left empty. |
| `price_decimal_shift` | dev | cpu, gpu, ram | yes | Price multiplied or divided by 100. |
| `vram_invalid` | dev | gpu | yes | VRAM size that no card ships with. |
| `negative_stock` | dev | cpu, gpu, ram | yes | Negative stock quantity. |
| `duplicate_row` | dev | cpu, gpu, ram | yes | Same supplier SKU appears twice. |
| `cosmetic_noise` | dev | cpu, gpu, ram | benign | Extra whitespace in brand or model name; should load after trimming. |
| `price_drift` | dev | cpu, gpu, ram | no | Price off by 8-20%, still plausible. Oracle limit. |
| `gtin_transposition` | heldout | cpu, gpu, ram | varies | Two adjacent GTIN digits swapped. |
| `socket_memory_mismatch` | heldout | cpu | yes | CPU memory support contradicts its socket. |
| `unit_slip_mb` | heldout | gpu, ram | yes | VRAM or RAM capacity given in MB instead of GB. |
| `spec_plausible_wrong` | heldout | cpu, gpu, ram | no | Spec value changed to another plausible value. Oracle limit. |

`price_drift` and `spec_plausible_wrong` are invisible by design: they measure the oracle's limits
(rows that pass every gate but are wrong). `gtin_transposition` is caught by the checksum unless the
swapped digits differ by 5; the log records which case each row is. `cosmetic_noise` is benign: the
row should load after trimming whitespace. For `duplicate_row` the later copy is the one expected to
be quarantined.

## Output layout

```
data/generated/
  manifest.json                       # config, seed, per-supplier format, provides, counts, input sha256
  dev/<supplier_id>/input/...         # the only files the pipeline may read
  dev/<supplier_id>/truth/ground_truth.csv
  dev/<supplier_id>/truth/corruptions.jsonl
  heldout/<supplier_id>/...
```

`ground_truth.csv`: `source_row, product_id, expected_outcome (load|quarantine), corruption`, then all
canonical columns with clean values. `source_row` is the 1-based data-row number (header excluded; for
JSON, the 1-based index into `items`).

Generation is deterministic: the same seed and config produce identical bytes.

```
supplygate generate --seed 42 --out data/generated
supplygate init-db --db data/supplygate.duckdb
```
