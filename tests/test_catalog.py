from collections import Counter
from decimal import Decimal

import pytest

from supplygate.catalog import load_catalog, parse_catalog
from supplygate.generator.corruptions import VALID_VRAM_GB
from supplygate.gtin import is_valid

CATALOG = load_catalog()
SOCKET_MEMORY = {"AM4": {"DDR4"}, "AM5": {"DDR5"}, "LGA1700": {"DDR4/DDR5"}, "LGA1851": {"DDR5"}}


def by_cat(cat):
    return [p for p in CATALOG if p.category == cat]


def test_catalog_has_all_categories():
    counts = Counter(p.category.value for p in CATALOG)
    assert counts["cpu"] >= 10 and counts["gpu"] >= 10 and counts["ram"] >= 8


def test_ids_gtins_mpns_unique():
    for key in ("product_id", "gtin"):
        values = [getattr(p, key) for p in CATALOG]
        assert len(values) == len(set(values)), key
    mpns = [p.mpn for p in CATALOG if p.mpn]
    assert len(mpns) == len(set(mpns))


def test_gtins_are_valid_internal_use():
    assert all(is_valid(p.gtin) and p.gtin.startswith("200") for p in CATALOG)


def test_every_product_is_a_valid_canonical_offer():
    for p in CATALOG:
        p.to_offer("test", p.product_id, Decimal("1.00"), 1)


@pytest.mark.parametrize("p", by_cat("cpu"), ids=lambda p: p.product_id)
def test_cpu_specs_are_consistent(p):
    a = p.attributes
    assert a["cpu_threads"] in (a["cpu_cores"], 2 * a["cpu_cores"]) or a["cpu_cores"] < a["cpu_threads"] < 2 * a["cpu_cores"]
    assert 1500 <= a["cpu_base_clock_mhz"] < a["cpu_boost_clock_mhz"] <= 6200
    assert a["cpu_memory_support"] in SOCKET_MEMORY[a["cpu_socket"]]


@pytest.mark.parametrize("p", by_cat("gpu"), ids=lambda p: p.product_id)
def test_gpu_specs_are_plausible(p):
    a = p.attributes
    assert a["gpu_vram_gb"] in VALID_VRAM_GB
    assert 1500 <= a["gpu_boost_clock_mhz"] <= 3200
    assert 75 <= a["gpu_tdp_w"] <= 600


@pytest.mark.parametrize("p", by_cat("ram"), ids=lambda p: p.product_id)
def test_ram_specs_are_consistent(p):
    a = p.attributes
    assert a["ram_capacity_gb"] % a["ram_modules"] == 0
    assert a["ram_type"] in p.model_name and str(a["ram_speed_mts"]) in p.model_name
    assert f"CL{a['ram_cas_latency']}" in p.model_name
    lo, hi = {"DDR4": (2133, 4800), "DDR5": (4800, 8400)}[a["ram_type"]]
    assert lo <= a["ram_speed_mts"] <= hi


def test_parse_rejects_unknown_and_missing_keys():
    item = {"product_id": "x", "brand": "b", "model_name": "m", "mpn": None, "reference_price_eur": 1, "verified": False,
            "type": "DDR4", "capacity_gb": 16, "modules": 2, "speed_mts": 3200, "cas_latency": 16}  # fmt: skip
    parse_catalog({"ram": [item]})
    with pytest.raises(ValueError, match="unknown"):
        parse_catalog({"ram": [item | {"colour": "black"}]})
    with pytest.raises(ValueError, match="missing"):
        parse_catalog({"ram": [{k: v for k, v in item.items() if k != "modules"}]})
