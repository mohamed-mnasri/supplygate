"""Reference decoders for the synthetic feeds, used only by tests.

They prove the generator's claim that every column in ``provides`` can be
recovered exactly from the feed. They are deliberately hand-written per supplier
and are not the mapping engine.
"""

from __future__ import annotations

import csv
import io
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from supplygate.generator.suppliers import (
    ERP_BRAND_CODES,
    ERP_CATEGORY_CODES,
    ERP_MEMORY_CODES,
    ERP_SOCKET_CODES,
    ERP_VRAM_CODES,
    SUPPLIERS,
)

CENT = Decimal("0.01")


def _int(s: str) -> int | None:
    s = s.strip()
    return int(s) if s else None


def _unit_int(s: str) -> int | None:
    m = re.fullmatch(r"\s*(\d+)\s*\D*", s)
    return int(m.group(1)) if m else None


def _ghz_to_mhz(s: str) -> int | None:
    s = s.strip().replace(",", ".")
    return int(Decimal(s) * 1000) if s else None


def _none(s: str) -> str | None:
    return s if s != "" else None


def _rows(path: Path, encoding: str, delimiter: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding=encoding), newline=""), delimiter=delimiter))


def _eur(native: Decimal | None, supplier_id: str) -> Decimal | None:
    return None if native is None else SUPPLIERS[supplier_id].price_to_eur(native)


def _price_comma(s: str) -> Decimal | None:
    s = s.replace(".", "").replace("\xa0", "").replace(",", ".")
    return Decimal(s) if s else None


def _cat(value: str, table: dict[str, str]) -> str:
    return table[value]


def decode_de(path: Path) -> list[dict[str, Any]]:
    cats = {"Prozessoren": "cpu", "Grafikkarten": "gpu", "Arbeitsspeicher": "ram"}
    out = []
    for r in _rows(path / "feed.csv", "utf-8-sig", ";"):
        out.append({
            "supplier_sku": r["Artikelnummer"], "gtin": _none(r["EAN"]), "mpn": _none(r["Herstellernummer"]),
            "brand": r["Hersteller"], "model_name": r["Bezeichnung"], "category": cats[r["Warengruppe"]],
            "price_eur": _eur(_price_comma(r["Preis (EUR)"]), "de_csv"), "stock_qty": _int(r["Bestand"]),
            "cpu_socket": _none(r["Sockel"]), "cpu_cores": _int(r["Kerne"]), "cpu_threads": _int(r["Threads"]),
            "cpu_base_clock_mhz": _ghz_to_mhz(r["Basistakt (GHz)"]), "cpu_boost_clock_mhz": _ghz_to_mhz(r["Turbotakt (GHz)"]),
            "cpu_tdp_w": _unit_int(r["TDP"]), "cpu_memory_support": _none(r["Speicherunterstützung"]),
            "gpu_chipset": _none(r["Grafikchip"]),
            "gpu_vram_gb": _unit_int(r["Grafikspeicher"]), "gpu_vram_type": _none(r["Grafikspeichertyp"]),
            "gpu_boost_clock_mhz": _int(r["GPU-Boost (MHz)"]), "gpu_tdp_w": _unit_int(r["Leistungsaufnahme"]),
            "ram_type": _none(r["Speichertyp"]), "ram_capacity_gb": _unit_int(r["Kapazität"]), "ram_modules": _int(r["Module"]),
            "ram_speed_mts": _unit_int(r["Speichertakt"]),
            "ram_cas_latency": _int(r["Latenz"].removeprefix("CL")),
        })  # fmt: skip
    return out


def decode_fr(path: Path) -> list[dict[str, Any]]:
    cats = {"Processeur": "cpu", "Carte graphique": "gpu", "Mémoire vive": "ram"}
    out = []
    for r in _rows(path / "feed.csv", "latin-1", ";"):
        out.append({
            "supplier_sku": r["Référence"], "gtin": _none(r["Code EAN"]), "mpn": _none(r["Réf. fabricant"]),
            "brand": r["Marque"], "model_name": r["Désignation"], "category": cats[r["Catégorie"]],
            "price_eur": _eur(_price_comma(r["Prix TTC (EUR)"]), "fr_csv"), "stock_qty": _int(r["Quantité"]),
            "cpu_socket": _none(r["Socket"]), "cpu_cores": _int(r["Nb coeurs"]), "cpu_threads": _int(r["Nb threads"]),
            "cpu_base_clock_mhz": _ghz_to_mhz(r["Fréquence de base (GHz)"]),
            "cpu_boost_clock_mhz": _ghz_to_mhz(r["Fréquence turbo (GHz)"]), "cpu_tdp_w": _int(r["TDP (W)"]),
            "cpu_memory_support": _none(r["Mémoire supportée"]),
            "gpu_chipset": _none(r["Puce graphique"]), "gpu_vram_gb": _unit_int(r["Mémoire vidéo"]),
            "gpu_vram_type": _none(r["Type mémoire vidéo"]), "gpu_boost_clock_mhz": _int(r["Fréquence GPU (MHz)"]),
            "gpu_tdp_w": _int(r["Consommation (W)"]), "ram_type": _none(r["Type de mémoire"]),
            "ram_capacity_gb": _unit_int(r["Capacité"]), "ram_modules": _int(r["Nombre de barrettes"]),
            "ram_speed_mts": _int(r["Fréquence mémoire (MHz)"]), "ram_cas_latency": _int(r["Latence CAS"]),
        })  # fmt: skip
    return out


def decode_uk(path: Path) -> list[dict[str, Any]]:
    cats = {"Processors": "cpu", "Graphics Cards": "gpu", "Memory": "ram"}
    out = []
    for r in _rows(path / "feed.csv", "utf-8", ","):
        price = r["Price (GBP)"].replace("£", "").replace(",", "")
        clock = r["Memory Clock (MHz)"]
        out.append({
            "supplier_sku": r["SKU"], "gtin": _none(r["Barcode"]), "mpn": _none(r["MPN"]), "brand": r["Manufacturer"],
            "model_name": r["Product Name"], "category": cats[r["Category"]],
            "price_eur": _eur(Decimal(price) if price else None, "uk_csv"), "stock_qty": _int(r["Stock"]),
            "cpu_socket": _none(r["CPU Socket"]), "cpu_cores": _int(r["Cores"]), "cpu_threads": _int(r["Threads"]),
            "cpu_base_clock_mhz": _int(r["Base Clock (MHz)"]), "cpu_boost_clock_mhz": _int(r["Boost Clock (MHz)"]),
            "cpu_tdp_w": _int(r["TDP (W)"]), "cpu_memory_support": _none(r["Memory Support"]), "gpu_chipset": _none(r["GPU"]),
            "gpu_vram_gb": _unit_int(r["VRAM"]), "gpu_vram_type": _none(r["VRAM Type"]),
            "gpu_boost_clock_mhz": _int(r["GPU Boost (MHz)"]), "gpu_tdp_w": _int(r["Board Power (W)"]),
            "ram_type": _none(r["Memory Type"]), "ram_capacity_gb": _unit_int(r["Capacity"]), "ram_modules": _int(r["Modules"]),
            "ram_speed_mts": int(Decimal(clock) * 2) if clock else None, "ram_cas_latency": _int(r["CAS"]),
        })  # fmt: skip
    return out


_CPU_RE = {
    "cpu_socket": (r"Socket (\w+)", str),
    "cpu_cores": (r"(\d+)C/\d+T", int),
    "cpu_threads": (r"\d+C/(\d+)T", int),
    "cpu_base_clock_mhz": (r"([\d.]+)-[\d.]+ GHz", lambda s: int(Decimal(s) * 1000)),
    "cpu_boost_clock_mhz": (r"[\d.]+-([\d.]+) GHz", lambda s: int(Decimal(s) * 1000)),
    "cpu_tdp_w": (r"(\d+)W TDP", int),
    "cpu_memory_support": (r"\| (DDR4/DDR5|DDR4|DDR5)$", str),
}
_GPU_RE = {
    "gpu_vram_gb": (r"\| (\d+)GB GDDR", int),
    "gpu_vram_type": (r"\| \d+GB (GDDR\w+)", str),
    "gpu_boost_clock_mhz": (r"Boost (\d+) MHz", int),
    "gpu_tdp_w": (r"\| (\d+)W$", int),
}
_RAM_RE = {
    "ram_type": (r"\| (DDR\d)-\d+ \|", str),
    "ram_speed_mts": (r"\| DDR\d-(\d+) \|", int),
    "ram_capacity_gb": (r"\| (\d+)GB kit", int),
    "ram_modules": (r"(\d+) modules", int),
    "ram_cas_latency": (r"\| CL(\d+)$", int),
}


def decode_spec_txt(path: Path, catalog_by_gtin: dict[str, Any]) -> list[dict[str, Any]]:
    """SPEC_TXT has no category column; tests use the GTIN to find brand/model/category boundaries."""
    out = []
    for r in _rows(path / "feed.csv", "utf-8", ","):
        text = r["SPEC_TXT"]
        parts = text.split(" | ")
        cat = "cpu" if "Socket" in text or "C/" in text else "gpu" if "GDDR" in text or "Boost" in text else "ram"
        row: dict[str, Any] = {
            "supplier_sku": r["item_code"], "gtin": _none(r["ean"]), "category": cat,
            "price_eur": _eur(Decimal(r["price_eur"]) if r["price_eur"] else None, "spec_txt"), "stock_qty": _int(r["qty"]),
            "_head": parts[0],
        }  # fmt: skip
        if cat == "gpu":
            row["gpu_chipset"] = parts[1]
        for col, (pattern, conv) in {"cpu": _CPU_RE, "gpu": _GPU_RE, "ram": _RAM_RE}[cat].items():
            m = re.search(pattern, text)
            row[col] = conv(m.group(1)) if m else None
        out.append(row)
    return out


def decode_erp(path: Path) -> list[dict[str, Any]]:
    inv = lambda table: {v: k for k, v in table.items()}  # noqa: E731
    brands, sockets, mems, vrams = inv(ERP_BRAND_CODES), inv(ERP_SOCKET_CODES), inv(ERP_MEMORY_CODES), inv(ERP_VRAM_CODES)
    cats = {code: cat.value for cat, code in ERP_CATEGORY_CODES.items()}
    out = []
    for r in _rows(path / "feed.txt", "cp1252", "|"):
        cat = cats[r["WGR"]]
        a = [r[f"A{i}"] for i in range(1, 8)]
        row: dict[str, Any] = {
            "supplier_sku": r["ARTNR"], "gtin": _none(r["EAN13"]), "mpn": _none(r["HSTNR"]), "brand": brands[r["HST_CD"]],
            "category": cat, "price_eur": _eur(Decimal(r["VK_CT"]) if r["VK_CT"] else None, "legacy_erp"),
            "stock_qty": _int(r["BESTAND"]),
        }  # fmt: skip
        if cat == "cpu":
            row |= {"cpu_socket": sockets.get(a[0]), "cpu_cores": _int(a[1]), "cpu_threads": _int(a[2]),
                    "cpu_base_clock_mhz": _int(a[3]), "cpu_boost_clock_mhz": _int(a[4]), "cpu_tdp_w": _int(a[5]),
                    "cpu_memory_support": mems.get(a[6])}  # fmt: skip
        elif cat == "gpu":
            row |= {"gpu_chipset": _none(a[0]), "gpu_vram_gb": None if not a[1] else int(a[1]) // 1024,
                    "gpu_vram_type": vrams.get(a[2]), "gpu_boost_clock_mhz": _int(a[3]), "gpu_tdp_w": _int(a[4])}  # fmt: skip
        else:
            row |= {"ram_type": mems.get(a[0]), "ram_capacity_gb": _int(a[1]), "ram_modules": _int(a[2]),
                    "ram_speed_mts": _int(a[3]), "ram_cas_latency": _int(a[4])}  # fmt: skip
        out.append(row)
    return out


def decode_json(path: Path) -> list[dict[str, Any]]:
    types = {"processor": "cpu", "graphics_card": "gpu", "memory": "ram"}
    names = {
        "socket": "cpu_socket", "core_count": "cpu_cores", "thread_count": "cpu_threads",
        "base_frequency": "cpu_base_clock_mhz", "max_turbo_frequency": "cpu_boost_clock_mhz", "tdp": "cpu_tdp_w",
        "memory_types": "cpu_memory_support", "chipset": "gpu_chipset", "memory_size": "gpu_vram_gb",
        "boost_clock": "gpu_boost_clock_mhz", "total_board_power": "gpu_tdp_w", "kit_capacity": "ram_capacity_gb",
        "module_count": "ram_modules", "data_rate": "ram_speed_mts", "cas_latency": "ram_cas_latency",
    }  # fmt: skip
    out = []
    for item in json.loads((path / "feed.json").read_text(encoding="utf-8"))["items"]:
        cat = types[item["type"]]
        price = item["offer"]["price"]
        row: dict[str, Any] = {
            "supplier_sku": item["id"], "gtin": item["identifiers"]["gtin"], "mpn": item["identifiers"]["mpn"],
            "brand": item["manufacturer"]["name"], "model_name": item["title"], "category": cat,
            "price_eur": _eur(Decimal(price["amount"]) if price else None, "json_api"),
            "stock_qty": item["offer"]["stock"]["available"],
        }  # fmt: skip
        for spec in item["specs"]:
            name, value = spec["name"], spec["value"]
            memory_col = "gpu_vram_type" if cat == "gpu" else "ram_type"
            col = memory_col if name == "memory_type" else names[name]
            if col == "cpu_memory_support":
                value = "/".join(value)
            elif spec.get("unit") == "GHz":
                value = int(Decimal(str(value)) * 1000)
            row[col] = value
        out.append(row)
    return out


DECODERS = {
    "de_csv": decode_de,
    "fr_csv": decode_fr,
    "uk_csv": decode_uk,
    "legacy_erp": decode_erp,
    "json_api": decode_json,
}
