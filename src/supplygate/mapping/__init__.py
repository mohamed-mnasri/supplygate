"""Declarative mapping spec (YAML) and its compiler to DuckDB SQL."""

from supplygate.mapping.compiler import CompiledMapping, compile_spec, run_mapping
from supplygate.mapping.spec import RISKY_OPS, MappingSpec, load_spec

__all__ = [
    "RISKY_OPS",
    "CompiledMapping",
    "MappingSpec",
    "compile_spec",
    "load_spec",
    "run_mapping",
]
