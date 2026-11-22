"""Command line entry point: ``supplygate <command>``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from supplygate.generator import SUPPLIERS, GenerationConfig, generate
from supplygate.schema import create_tables


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="supplygate")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Generate synthetic supplier feeds with ground truth and corruption logs.")
    gen.add_argument("--out", type=Path, default=Path("data/generated"))
    gen.add_argument("--seed", type=int, default=GenerationConfig.seed)
    gen.add_argument("--corruption-rate", type=float, default=GenerationConfig.corruption_rate)
    gen.add_argument("--supplier", action="append", choices=sorted(SUPPLIERS), help="Repeat to pick suppliers (default: all).")

    init = sub.add_parser("init-db", help="Create the canonical tables in a DuckDB file.")
    init.add_argument("--db", type=Path, default=Path("data/supplygate.duckdb"))

    args = parser.parse_args(argv)
    if args.command == "generate":
        config = GenerationConfig(
            seed=args.seed,
            corruption_rate=args.corruption_rate,
            suppliers=tuple(args.supplier) if args.supplier else GenerationConfig.suppliers,
        )
        manifest = generate(args.out, config)
        for sid, info in manifest["suppliers"].items():
            print(f"{info['split']:8} {sid:11} rows={info['rows']:3} corrupted={info['corrupted_rows']:2} "
                  f"expected_quarantine={info['expected_quarantine']:2}")  # fmt: skip
        print(f"wrote {args.out / 'manifest.json'}")
    elif args.command == "init-db":
        args.db.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(args.db)) as con:
            create_tables(con)
        print(f"created canonical tables in {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
