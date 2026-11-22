"""Seed catalog: hand-curated real component specs (ground truth for generation)."""

from __future__ import annotations

from decimal import Decimal
from functools import cache
from importlib.resources import files
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from supplygate.gtin import synthetic_gtin
from supplygate.schema import CanonicalOffer, Category

# seed YAML key -> canonical column, per category
_ATTR_COLUMNS: dict[Category, dict[str, str]] = {
    Category.CPU: {
        "socket": "cpu_socket",
        "cores": "cpu_cores",
        "threads": "cpu_threads",
        "base_clock_mhz": "cpu_base_clock_mhz",
        "boost_clock_mhz": "cpu_boost_clock_mhz",
        "tdp_w": "cpu_tdp_w",
        "memory_support": "cpu_memory_support",
    },
    Category.GPU: {
        "chipset": "gpu_chipset",
        "vram_gb": "gpu_vram_gb",
        "vram_type": "gpu_vram_type",
        "boost_clock_mhz": "gpu_boost_clock_mhz",
        "tdp_w": "gpu_tdp_w",
    },
    Category.RAM: {
        "type": "ram_type",
        "capacity_gb": "ram_capacity_gb",
        "modules": "ram_modules",
        "speed_mts": "ram_speed_mts",
        "cas_latency": "ram_cas_latency",
    },
}

_BASE_KEYS = {"product_id", "brand", "model_name", "mpn", "reference_price_eur", "verified"}


class CatalogProduct(BaseModel):
    """A real product from the seed catalog. ``attributes`` uses canonical column names."""

    model_config = ConfigDict(frozen=True)

    product_id: str
    category: Category
    brand: str
    model_name: str
    mpn: str | None
    gtin: str = Field(description="Synthetic GTIN-13, prefix 200.")
    reference_price_eur: Decimal
    verified: bool
    attributes: dict[str, Any]

    def to_offer(self, supplier_id: str, supplier_sku: str, price_eur: Decimal | None, stock_qty: int | None) -> CanonicalOffer:
        """Clean canonical offer of this product by a supplier."""
        return CanonicalOffer(
            supplier_id=supplier_id,
            supplier_sku=supplier_sku,
            category=self.category,
            gtin=self.gtin,
            mpn=self.mpn,
            brand=self.brand,
            model_name=self.model_name,
            price_eur=price_eur,
            stock_qty=stock_qty,
            **self.attributes,
        )


def parse_catalog(raw: dict[str, list[dict[str, Any]]]) -> list[CatalogProduct]:
    products = []
    for cat_key, items in raw.items():
        category = Category(cat_key)
        mapping = _ATTR_COLUMNS[category]
        for item in items:
            unknown = set(item) - _BASE_KEYS - set(mapping)
            if unknown:
                raise ValueError(f"{item.get('product_id')}: unknown keys {sorted(unknown)}")
            missing = set(mapping) - set(item)
            if missing:
                raise ValueError(f"{item.get('product_id')}: missing attributes {sorted(missing)}")
            products.append(
                CatalogProduct(
                    product_id=item["product_id"],
                    category=category,
                    brand=item["brand"],
                    model_name=item["model_name"],
                    mpn=item.get("mpn"),
                    gtin=synthetic_gtin(item["product_id"]),
                    reference_price_eur=Decimal(str(item["reference_price_eur"])),
                    verified=bool(item.get("verified", False)),
                    attributes={col: item[key] for key, col in mapping.items()},
                )
            )
    return products


@cache
def load_catalog() -> tuple[CatalogProduct, ...]:
    """Load the packaged seed catalog."""
    text = files("supplygate.seed").joinpath("catalog.yaml").read_text(encoding="utf-8")
    return tuple(parse_catalog(yaml.safe_load(text)))
