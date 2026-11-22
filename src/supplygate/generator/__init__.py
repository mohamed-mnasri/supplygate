"""Synthetic supplier-feed generator."""

from supplygate.generator.generate import GenerationConfig, generate, generate_supplier
from supplygate.generator.suppliers import SUPPLIERS

__all__ = ["SUPPLIERS", "GenerationConfig", "generate", "generate_supplier"]
