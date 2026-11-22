# SupplyGate

A supplier-data onboarding engine for a PC-components retailer. Messy supplier feeds (different languages, encodings, units, formats) are mapped into one canonical catalog in DuckDB. LLM-proposed mappings are compiled from a declarative spec and checked by independent, deterministic gates.

> **Status: early scaffold.** Nothing is implemented yet. This README describes the plan; results will be added only when they are measured.

## Idea

- **Contract-first mapping.** The mapping is a small declarative spec (YAML), compiled to DuckDB SQL. The LLM never writes free-form code.
- **Memory that improves.** Approved mappings are stored and reused, so each new supplier needs fewer human corrections and fewer LLM calls.
- **Independent gates.** A plausibility oracle (GTIN checksums, valid VRAM sizes, socket/memory compatibility, price ranges) and a row-conservation check (`rows_in = loaded + quarantined`) that the mapping step cannot modify.
- **Small local model.** Runs with `qwen3.5:4b` through Ollama on a mid-range laptop. Zero cost. LLM answers are cached.

## Planned evaluation

- Learning curve (corrections and LLM calls per supplier) over random onboarding orders, with confidence intervals
- Ablations: cold vs warm memory, with vs without the oracle, lexical vs embedding vs LLM matching
- False-pass rate: rows that passed the gates but are wrong against ground truth
- Reward-hacking test: risky spec operations (`filter_rows`, `default_value`, `null_if`, `cast_loose`) and how often the gates catch them
- Held-out supplier formats that are not used during development

## Data

Supplier feeds are synthetic, generated from a small hand-curated catalog of real component specs, with logged corruptions. Real supplier feeds are not public, and the README will say so.

## Stack

Python 3.12+, DuckDB, Pydantic, PyYAML, Ollama, pytest, GitHub Actions.

## Setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

## Roadmap

1. Canonical schema, seed catalog, supplier generator with corruption log and held-out split
2. Profiler, mapping DSL and compiler
3. Oracle, quarantine, conservation check, lineage
4. Matcher, LLM tie-break with cache and fake provider for tests
5. Mapping memory and confidence router
6. Experiments, ablations, reward-hacking test
7. Eval report and write-up of failures and trade-offs

See [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) for design decisions.

## License

MIT
