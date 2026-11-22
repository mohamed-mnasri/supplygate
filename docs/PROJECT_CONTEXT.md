# SupplyGate: project context

Summary of the planning conversation, so it can be pasted into a Claude Project as starting context.

## Goal
A portfolio project for Data Engineer / AI Engineer / Forward Deployed Engineer roles. It must be 100% free (no paid APIs, servers or subscriptions), run locally, and not look like a generic AI-generated demo. Target time: about one week (9-10 days realistic).

## Owner's setup
- Windows 11, i7-10750H, 32 GB RAM, RTX 2060 (6 GB VRAM), mid-range laptop
- Ollama with `qwen3.5:4b` already installed and used in the EuroGrid project
- Comfortable with SQL, Python and Docker

## Existing project: EuroGrid (already done)
Local data and AI platform for European electricity markets: Python ingestion, DuckDB, dbt (42 tests), FastAPI, React, LangGraph agent with Ollama `qwen3.5:4b`, code-verified LLM output, chaos mode, synthetic eval cases (7). It covers read-side analytics, fault injection and verified LLM output.

## What SupplyGate adds (not in EuroGrid)
- Write-side integration: heterogeneous files into a canonical model
- Declarative mapping DSL + compiler (LLM output is reviewable and diffable)
- Embeddings/lexical matching and retrieval as a system component (mapping memory)
- Learning-curve evaluation with random orderings, ablations, confidence intervals
- Row-conservation invariant, quarantine, cell-level lineage
- Adversarial test of risky spec operations (reward hacking)
- Human-in-the-loop review queue with a confidence router

## Design decisions
1. **Domain:** PC components (CPU, GPU, RAM). Category-specific attributes (socket, VRAM, DDR generation...) make mapping harder than renaming columns.
2. **LLM role is narrow.** A deterministic matcher shortlists 3-5 candidates per source column; the LLM only picks one for ambiguous columns, one column at a time, JSON-constrained, cached by prompt hash.
3. **Gates the mapping step cannot touch:** plausibility oracle (GTIN checksum, valid VRAM, socket/DDR compatibility, price ranges) and conservation check (`rows_in = loaded + quarantined`).
4. **Evaluation fixes:** held-out corruption families (developed and scored separately); report oracle limits (errors it cannot see); reward-hacking test with risky ops `filter_rows`, `default_value`, `null_if`, `cast_loose` and per-op catch rate.
5. **No LangGraph, Kafka, Airflow, MCP, Kubernetes, vector DB, dbt** in this project; none is justified by the requirements.
6. **Data:** supplier feeds are synthetic (real ones are not public), generated from a hand-curated seed of real component specs (verify against manufacturer pages before publishing). Synthetic GTINs use the internal-use prefix 200 with valid check digits.
7. **Optional, cut in this order:** review UI (CLI first), drift scenario, food zero-shot domain (Open Food Facts), free-text `SPEC_TXT` LLM handling.

## Laptop-friendly settings
`OLLAMA_NUM_PARALLEL=1`, one loaded model, `num_ctx` about 2048, short outputs, thinking off if supported, cache everything, batch LLM work with pauses, tests and CI use a fake LLM.

## Honest claims policy
Do not claim: multi-agent system, production-ready, generalizes to any domain, accuracy on real supplier data. Report only measured numbers.

## Planned suppliers (6)
Dev split: German CSV (`;`, decimal comma), French CSV (Latin-1, translated headers), UK CSV (GBP, MHz units).
Held-out split: free-text `SPEC_TXT`, legacy ERP (cryptic codes, cents, code tables), nested JSON API.

## Next step
Roadmap item 1: canonical schema, seed catalog, supplier generator with corruption log and held-out split.
