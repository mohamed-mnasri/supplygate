"""Seed catalog: the list of real products every synthetic supplier sells.

The catalog lives in ``seed/catalog.yaml`` and is the ground truth for the
generator: each supplier feed is "these products, priced and rendered in that
supplier's format, with a few errors added".

The YAML uses short keys per category (``socket``, ``vram_gb``) because it is
edited by hand. This module translates them to canonical column names
(``cpu_socket``, ``gpu_vram_gb``) and attaches a synthetic GTIN.

    for product in load_catalog():
        offer = product.to_offer("de_csv", "DE-100000", Decimal("399.90"), stock_qty=12)
"""

from __future__ import annotations

from decimal import Decimal
from functools import cache
from importlib.resources import files
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from supplygate.gtin import synthetic_gtin
from supplygate.schema import CanonicalOffer, Category

# Seed YAML key -> canonical column, per category. A key not listed here is an
# error (see parse_catalog), so a typo in the YAML fails loudly.
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

# Keys every product has, whatever its category.
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
    """Turn the YAML structure ``{category: [product, ...]}`` into CatalogProduct objects."""
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
                    # Not a real barcode: derived from product_id, see gtin.py.
                    gtin=synthetic_gtin(item["product_id"]),
                    # str() first: Decimal(0.1) would carry float rounding noise.
                    reference_price_eur=Decimal(str(item["reference_price_eur"])),
                    verified=bool(item.get("verified", False)),
                    attributes={col: item[key] for key, col in mapping.items()},
                )
            )
    return products


@cache
def load_catalog() -> tuple[CatalogProduct, ...]:
    """Load the packaged seed catalog (parsed once, then cached).

    Read through importlib.resources so it also works from an installed wheel,
    not only from a source checkout.
    """
    text = files("supplygate.seed").joinpath("catalog.yaml").read_text(encoding="utf-8")
    return tuple(parse_catalog(yaml.safe_load(text)))
