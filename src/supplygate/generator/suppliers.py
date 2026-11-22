"""The six synthetic supplier formats.

Each supplier renders canonical rows into its own file format. Rendering is
deterministic and, for the columns listed in ``provides``, lossless: a correct
mapping can recover the canonical value exactly. Columns not in ``provides`` are
absent or lossy in that feed and are not scored for that supplier.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from supplygate.generator.corruptions import RowState
from supplygate.schema import Category, canonical_columns

CENT = Decimal("0.01")
ALL_COLUMNS = tuple(canonical_columns())


def _eur(native: Decimal) -> Decimal:
    return native.quantize(CENT, ROUND_HALF_UP)


@dataclass(frozen=True)
class Supplier:
    """Everything that defines one supplier: its format, split and how to render rows."""

    supplier_id: str
    split: str
    description: str
    currency: str
    fx_to_eur: Decimal  # canonical EUR = round(native * fx_to_eur, 2)
    price_in_cents: bool
    files: dict[str, str]  # file name -> encoding
    format: str
    delimiter: str | None
    provides: tuple[str, ...]
    sku: Callable[[int, str], str]
    render: Callable[[list[RowState]], dict[str, bytes]]
    excluded_families: frozenset[str] = frozenset()  # families this format would erase

    def price_to_eur(self, native: Decimal) -> Decimal:
        """Convert a price as written in the feed to canonical EUR (2 decimals)."""
        if self.price_in_cents:
            native = native / 100
        return _eur(native * self.fx_to_eur)


# --- helpers ------------------------------------------------------------------------------------


def _v(row: RowState, col: str) -> Any:
    return row.values.get(col)


def _blank(x: Any) -> str:
    return "" if x is None else str(x)


def _int_or_blank(x: Any, suffix: str = "") -> str:
    return "" if x is None else f"{x}{suffix}"


def _ghz(mhz: int | None, decimal_sep: str = ".") -> str:
    # Decimal, not float: 2475 / 1000 must print as "2.475", never "2.4749999".
    """Exact GHz string for an integer MHz value (4700 -> '4.7', 2475 -> '2.475')."""
    if mhz is None:
        return ""
    text = format((Decimal(mhz) / 1000).normalize(), "f")
    if "." not in text:
        text += ".0"
    return text.replace(".", decimal_sep)


def _decimal_comma(value: Decimal | None, thousands: str) -> str:
    """1234.56 -> "1.234,56" (thousands=".") or "1\xa0234,56" (thousands="\xa0")."""
    if value is None:
        return ""
    text = f"{value:,.2f}"  # 1,234.56
    return text.replace(",", "\0").replace(".", ",").replace("\0", thousands)


def _csv_bytes(header: list[str], rows: list[list[str]], delimiter: str, encoding: str) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode(encoding)


def _hash_sku(prefix: str, n: int, product_id: str, length: int) -> str:
    return prefix + hashlib.sha1(f"{prefix}{n}{product_id}".encode()).hexdigest()[:length].upper()


# --- de_csv: German shop export ------------------------------------------------------------------
# What a mapper has to handle: German headers, ';' separator, UTF-8 with BOM,
# "1.234,56" prices, clocks in GHz with a decimal comma ("4,7" = 4700 MHz),
# units inside cells ("105 W", "12 GB", "CL30"), and "MHz" used for RAM speed
# where the number is really MT/s (a common shop habit).

_DE_CATEGORY = {Category.CPU: "Prozessoren", Category.GPU: "Grafikkarten", Category.RAM: "Arbeitsspeicher"}
_DE_HEADER = [
    "Artikelnummer", "EAN", "Herstellernummer", "Hersteller", "Bezeichnung", "Warengruppe", "Preis (EUR)",
    "Bestand", "Sockel", "Kerne", "Threads", "Basistakt (GHz)", "Turbotakt (GHz)", "TDP",
    "Speicherunterstützung", "Grafikchip", "Grafikspeicher", "Grafikspeichertyp", "GPU-Boost (MHz)", "Leistungsaufnahme",
    "Speichertyp", "Kapazität", "Module", "Speichertakt", "Latenz",
]  # fmt: skip


def _render_de(rows: list[RowState]) -> dict[str, bytes]:
    out = []
    for r in rows:
        tdp = _v(r, "cpu_tdp_w")
        out.append([
            _v(r, "supplier_sku"), _blank(_v(r, "gtin")), _blank(_v(r, "mpn")), _v(r, "brand"), _v(r, "model_name"),
            _DE_CATEGORY[_v(r, "category")], _decimal_comma(r.native_price, "."), _blank(_v(r, "stock_qty")),
            _blank(_v(r, "cpu_socket")), _blank(_v(r, "cpu_cores")), _blank(_v(r, "cpu_threads")),
            _ghz(_v(r, "cpu_base_clock_mhz"), ","), _ghz(_v(r, "cpu_boost_clock_mhz"), ","), _int_or_blank(tdp, " W"),
            _blank(_v(r, "cpu_memory_support")), _blank(_v(r, "gpu_chipset")), _int_or_blank(_v(r, "gpu_vram_gb"), " GB"),
            _blank(_v(r, "gpu_vram_type")), _blank(_v(r, "gpu_boost_clock_mhz")),
            _int_or_blank(_v(r, "gpu_tdp_w"), " W"), _blank(_v(r, "ram_type")),
            _int_or_blank(_v(r, "ram_capacity_gb"), " GB"), _blank(_v(r, "ram_modules")),
            _int_or_blank(_v(r, "ram_speed_mts"), " MHz"),
            "" if _v(r, "ram_cas_latency") is None else f"CL{_v(r, 'ram_cas_latency')}",
        ])  # fmt: skip
    return {"feed.csv": _csv_bytes(_DE_HEADER, out, ";", "utf-8-sig")}


# --- fr_csv: French distributor, Latin-1 ------------------------------------------------------
# Latin-1 encoded (reading it as UTF-8 fails on the accents), French headers,
# decimal comma with a no-break space as thousands separator ("1\xa0234,56"),
# and "Go" (gigaoctet) instead of "GB".

_FR_CATEGORY = {Category.CPU: "Processeur", Category.GPU: "Carte graphique", Category.RAM: "Mémoire vive"}
_FR_HEADER = [
    "Référence", "Code EAN", "Réf. fabricant", "Marque", "Désignation", "Catégorie", "Prix TTC (EUR)",
    "Quantité", "Socket", "Nb coeurs", "Nb threads", "Fréquence de base (GHz)", "Fréquence turbo (GHz)",
    "TDP (W)", "Mémoire supportée", "Puce graphique", "Mémoire vidéo", "Type mémoire vidéo", "Fréquence GPU (MHz)",
    "Consommation (W)", "Type de mémoire", "Capacité", "Nombre de barrettes", "Fréquence mémoire (MHz)",
    "Latence CAS",
]  # fmt: skip


def _render_fr(rows: list[RowState]) -> dict[str, bytes]:
    out = []
    for r in rows:
        out.append([
            _v(r, "supplier_sku"), _blank(_v(r, "gtin")), _blank(_v(r, "mpn")), _v(r, "brand"), _v(r, "model_name"),
            _FR_CATEGORY[_v(r, "category")], _decimal_comma(r.native_price, "\xa0"), _blank(_v(r, "stock_qty")),
            _blank(_v(r, "cpu_socket")), _blank(_v(r, "cpu_cores")), _blank(_v(r, "cpu_threads")),
            _ghz(_v(r, "cpu_base_clock_mhz"), ","), _ghz(_v(r, "cpu_boost_clock_mhz"), ","),
            _blank(_v(r, "cpu_tdp_w")), _blank(_v(r, "cpu_memory_support")), _blank(_v(r, "gpu_chipset")),
            _int_or_blank(_v(r, "gpu_vram_gb"), " Go"), _blank(_v(r, "gpu_vram_type")),
            _blank(_v(r, "gpu_boost_clock_mhz")), _blank(_v(r, "gpu_tdp_w")), _blank(_v(r, "ram_type")),
            _int_or_blank(_v(r, "ram_capacity_gb"), " Go"), _blank(_v(r, "ram_modules")),
            _blank(_v(r, "ram_speed_mts")), _blank(_v(r, "ram_cas_latency")),
        ])  # fmt: skip
    return {"feed.csv": _csv_bytes(_FR_HEADER, out, ";", "latin-1")}


# --- uk_csv: UK wholesaler, GBP, true memory clock in MHz ---------------------------------------
# Prices in GBP with a pound sign; the mapping must convert with the fixed rate
# in Supplier.fx_to_eur. All clocks are in MHz. "Memory Clock (MHz)" is the real
# clock, half the data rate: DDR5-6000 is listed as 3000.

_UK_CATEGORY = {Category.CPU: "Processors", Category.GPU: "Graphics Cards", Category.RAM: "Memory"}
_UK_HEADER = [
    "SKU", "Barcode", "MPN", "Manufacturer", "Product Name", "Category", "Price (GBP)", "Stock",
    "CPU Socket", "Cores", "Threads", "Base Clock (MHz)", "Boost Clock (MHz)", "TDP (W)", "Memory Support",
    "GPU", "VRAM", "VRAM Type", "GPU Boost (MHz)", "Board Power (W)", "Memory Type", "Capacity", "Modules",
    "Memory Clock (MHz)", "CAS",
]  # fmt: skip


def _render_uk(rows: list[RowState]) -> dict[str, bytes]:
    out = []
    for r in rows:
        speed = _v(r, "ram_speed_mts")
        out.append([
            _v(r, "supplier_sku"), _blank(_v(r, "gtin")), _blank(_v(r, "mpn")), _v(r, "brand"), _v(r, "model_name"),
            _UK_CATEGORY[_v(r, "category")], "" if r.native_price is None else f"£{r.native_price:,.2f}",
            _blank(_v(r, "stock_qty")), _blank(_v(r, "cpu_socket")), _blank(_v(r, "cpu_cores")),
            _blank(_v(r, "cpu_threads")), _blank(_v(r, "cpu_base_clock_mhz")), _blank(_v(r, "cpu_boost_clock_mhz")),
            _blank(_v(r, "cpu_tdp_w")), _blank(_v(r, "cpu_memory_support")), _blank(_v(r, "gpu_chipset")),
            _int_or_blank(_v(r, "gpu_vram_gb"), "GB"), _blank(_v(r, "gpu_vram_type")),
            _blank(_v(r, "gpu_boost_clock_mhz")), _blank(_v(r, "gpu_tdp_w")), _blank(_v(r, "ram_type")),
            _int_or_blank(_v(r, "ram_capacity_gb"), "GB"), _blank(_v(r, "ram_modules")),
            # DDR transfers twice per clock: 6000 MT/s is a 3000 MHz memory clock.
            "" if speed is None else str(speed // 2) if speed % 2 == 0 else str(Decimal(speed) / 2),
            _blank(_v(r, "ram_cas_latency")),
        ])  # fmt: skip
    return {"feed.csv": _csv_bytes(_UK_HEADER, out, ",", "utf-8")}


# --- spec_txt: free-text spec column --------------------------------------------------------------
# Only id, EAN, price and stock are columns. Everything else is packed into one
# free-text field, e.g. "AMD Ryzen 7 7800X3D | Socket AM5 | 8C/16T | 4.2-5.0 GHz".
# There is no category column; it has to be inferred from the text.


def _spec_text(r: RowState) -> str:
    cat = _v(r, "category")
    parts = [f"{_v(r, 'brand')} {_v(r, 'model_name')}"]

    def add(fmt: str, *cols: str) -> None:
        vals = [_v(r, c) for c in cols]
        if all(v is not None for v in vals):
            parts.append(fmt.format(*vals))

    if cat == Category.CPU:
        add("Socket {}", "cpu_socket")
        add("{}C/{}T", "cpu_cores", "cpu_threads")
        base, boost = _v(r, "cpu_base_clock_mhz"), _v(r, "cpu_boost_clock_mhz")
        if base is not None and boost is not None:
            parts.append(f"{_ghz(base)}-{_ghz(boost)} GHz")
        add("{}W TDP", "cpu_tdp_w")
        add("{}", "cpu_memory_support")
    elif cat == Category.GPU:
        add("{}", "gpu_chipset")
        add("{}GB {}", "gpu_vram_gb", "gpu_vram_type")
        add("Boost {} MHz", "gpu_boost_clock_mhz")
        add("{}W", "gpu_tdp_w")
    else:
        add("{}-{}", "ram_type", "ram_speed_mts")
        add("{}GB kit, {} modules", "ram_capacity_gb", "ram_modules")
        add("CL{}", "ram_cas_latency")
    return " | ".join(parts)


def _render_spec_txt(rows: list[RowState]) -> dict[str, bytes]:
    out = [
        [_v(r, "supplier_sku"), _blank(_v(r, "gtin")), _blank(r.native_price), _blank(_v(r, "stock_qty")), _spec_text(r)]
        for r in rows
    ]
    return {"feed.csv": _csv_bytes(["item_code", "ean", "price_eur", "qty", "SPEC_TXT"], out, ",", "utf-8")}


# --- legacy_erp: cryptic codes, cents, positional attributes, code tables -------------------------
# An old ERP export: pipe-separated cp1252, short German abbreviations as headers
# (VK_CT = Verkaufspreis in Cent), brand/category/socket as codes that are
# explained in a separate code_tables.csv, and A1..A7 columns whose meaning
# depends on the category (A2 is cores for a CPU, VRAM in MB for a GPU,
# capacity in GB for RAM).

ERP_BRAND_CODES = {"AMD": "A01", "Intel": "I02", "NVIDIA": "N03", "Corsair": "C10", "G.Skill": "G11",
                   "Kingston": "K12", "Crucial": "M13"}  # fmt: skip
ERP_CATEGORY_CODES = {Category.CPU: "100", Category.GPU: "200", Category.RAM: "300"}
ERP_SOCKET_CODES = {"AM4": "S1", "AM5": "S2", "LGA1700": "S3", "LGA1851": "S4"}
ERP_MEMORY_CODES = {"DDR4": "M4", "DDR5": "M5", "DDR4/DDR5": "M45"}
ERP_VRAM_CODES = {"GDDR6": "V6", "GDDR6X": "V6X", "GDDR7": "V7"}
_ERP_HEADER = ["ARTNR", "EAN13", "HSTNR", "HST_CD", "BEZ", "WGR", "VK_CT", "BESTAND", "A1", "A2", "A3", "A4", "A5", "A6", "A7"]


def _code(table: dict[str, str], value: Any) -> str:
    return "" if value is None else table[value]


def _render_erp(rows: list[RowState]) -> dict[str, bytes]:
    out = []
    for r in rows:
        cat = _v(r, "category")
        if cat == Category.CPU:
            attrs = [_code(ERP_SOCKET_CODES, _v(r, "cpu_socket")), _blank(_v(r, "cpu_cores")), _blank(_v(r, "cpu_threads")),
                     _blank(_v(r, "cpu_base_clock_mhz")), _blank(_v(r, "cpu_boost_clock_mhz")), _blank(_v(r, "cpu_tdp_w")),
                     _code(ERP_MEMORY_CODES, _v(r, "cpu_memory_support"))]  # fmt: skip
        elif cat == Category.GPU:
            vram = _v(r, "gpu_vram_gb")
            attrs = [_blank(_v(r, "gpu_chipset")), "" if vram is None else str(vram * 1024),
                     _code(ERP_VRAM_CODES, _v(r, "gpu_vram_type")), _blank(_v(r, "gpu_boost_clock_mhz")),
                     _blank(_v(r, "gpu_tdp_w")), "", ""]  # fmt: skip
        else:
            attrs = [_code(ERP_MEMORY_CODES, _v(r, "ram_type")), _blank(_v(r, "ram_capacity_gb")), _blank(_v(r, "ram_modules")),
                     _blank(_v(r, "ram_speed_mts")), _blank(_v(r, "ram_cas_latency")), "", ""]  # fmt: skip
        bez = " ".join(str(_v(r, "model_name")).split()).upper()[:30]
        out.append([_v(r, "supplier_sku"), _blank(_v(r, "gtin")), _blank(_v(r, "mpn")), ERP_BRAND_CODES[_v(r, "brand").strip()],
                    bez, ERP_CATEGORY_CODES[cat], _blank(r.native_price), _blank(_v(r, "stock_qty")), *attrs])  # fmt: skip
    code_rows = []
    for name, table in [("HST", ERP_BRAND_CODES), ("WGR", {c.value: code for c, code in ERP_CATEGORY_CODES.items()}),
                        ("SOCKEL", ERP_SOCKET_CODES), ("SPEICHER", ERP_MEMORY_CODES), ("VRAMTYP", ERP_VRAM_CODES)]:  # fmt: skip
        code_rows += [[name, code, value] for value, code in table.items()]
    return {
        "feed.txt": _csv_bytes(_ERP_HEADER, out, "|", "cp1252"),
        "code_tables.csv": _csv_bytes(["TABLE", "CODE", "VALUE"], code_rows, ";", "cp1252"),
    }


# --- json_api: nested JSON with key/value spec lists ------------------------------------------------
# Nested objects (identifiers, offer.price.amount as a string) and specs as a
# list of {name, value, unit} entries instead of fixed fields. CPU memory
# support is a list (["DDR4", "DDR5"]).

_JSON_TYPE = {Category.CPU: "processor", Category.GPU: "graphics_card", Category.RAM: "memory"}
_JSON_SPECS: dict[Category, list[tuple[str, str, str | None, bool]]] = {
    # (spec name, canonical column, unit, as GHz)
    Category.CPU: [("socket", "cpu_socket", None, False), ("core_count", "cpu_cores", None, False),
                   ("thread_count", "cpu_threads", None, False), ("base_frequency", "cpu_base_clock_mhz", "GHz", True),
                   ("max_turbo_frequency", "cpu_boost_clock_mhz", "GHz", True), ("tdp", "cpu_tdp_w", "W", False),
                   ("memory_types", "cpu_memory_support", None, False)],
    Category.GPU: [("chipset", "gpu_chipset", None, False), ("memory_size", "gpu_vram_gb", "GB", False),
                   ("memory_type", "gpu_vram_type", None, False), ("boost_clock", "gpu_boost_clock_mhz", "MHz", False),
                   ("total_board_power", "gpu_tdp_w", "W", False)],
    Category.RAM: [("memory_type", "ram_type", None, False), ("kit_capacity", "ram_capacity_gb", "GB", False),
                   ("module_count", "ram_modules", None, False), ("data_rate", "ram_speed_mts", "MT/s", False),
                   ("cas_latency", "ram_cas_latency", None, False)],
}  # fmt: skip


def _render_json(rows: list[RowState]) -> dict[str, bytes]:
    items = []
    for r in rows:
        cat = _v(r, "category")
        specs = []
        for name, col, unit, as_ghz in _JSON_SPECS[cat]:
            value = _v(r, col)
            if value is None:
                continue
            if col == "cpu_memory_support":
                value = value.split("/")
            elif as_ghz:
                value = float(_ghz(value))
            spec: dict[str, Any] = {"name": name, "value": value}
            if unit:
                spec["unit"] = unit
            specs.append(spec)
        items.append({
            "id": _v(r, "supplier_sku"),
            "identifiers": {"gtin": _v(r, "gtin"), "mpn": _v(r, "mpn")},
            "manufacturer": {"name": _v(r, "brand")},
            "title": _v(r, "model_name"),
            "type": _JSON_TYPE[cat],
            "offer": {
                "price": None if r.native_price is None else {"amount": str(r.native_price), "currency": "EUR"},
                "stock": {"available": _v(r, "stock_qty")},
            },
            "specs": specs,
        })  # fmt: skip
    doc = {"api_version": "2.3", "page": 1, "page_count": 1, "items": items}
    return {"feed.json": (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")}


# --- registry ---------------------------------------------------------------------------------------

_NO_MPN = tuple(c for c in ALL_COLUMNS if c != "mpn")
_NO_MODEL = tuple(c for c in ALL_COLUMNS if c != "model_name")

SUPPLIERS: dict[str, Supplier] = {
    s.supplier_id: s
    for s in [
        Supplier("de_csv", "dev", "German shop export: ';' separator, decimal comma, German headers, clocks in GHz, units in cells.",
                 "EUR", Decimal(1), False, {"feed.csv": "utf-8-sig"}, "csv", ";", ALL_COLUMNS,
                 lambda n, pid: f"DE-{100000 + n * 7:06d}", _render_de),
        Supplier("fr_csv", "dev", "French distributor: Latin-1, French headers, decimal comma with no-break-space thousands, 'Go' units.",
                 "EUR", Decimal(1), False, {"feed.csv": "latin-1"}, "csv", ";", ALL_COLUMNS,
                 lambda n, pid: f"FR{20000 + n * 13:05d}", _render_fr),
        Supplier("uk_csv", "dev", "UK wholesaler: GBP prices with '£', clocks in MHz, memory clock in MHz (half the MT/s rate).",
                 "GBP", Decimal("1.17"), False, {"feed.csv": "utf-8"}, "csv", ",", ALL_COLUMNS,
                 lambda n, pid: _hash_sku("UK-", n, pid, 6), _render_uk),
        Supplier("spec_txt", "heldout", "Specs only in a free-text SPEC_TXT column; no MPN.",
                 "EUR", Decimal(1), False, {"feed.csv": "utf-8"}, "csv", ",", _NO_MPN,
                 lambda n, pid: f"ST{n + 1}", _render_spec_txt),
        Supplier("legacy_erp", "heldout", "Legacy ERP: pipe-separated cp1252, cryptic headers, prices in cents, code tables, "
                 "positional A1-A7 attributes per category, VRAM in MB, names truncated to 30 upper-case characters.",
                 "EUR", Decimal(1), True, {"feed.txt": "cp1252", "code_tables.csv": "cp1252"}, "csv", "|", _NO_MODEL,
                 lambda n, pid: f"{40000000 + n * 31:08d}", _render_erp, frozenset({"cosmetic_noise"})),
        Supplier("json_api", "heldout", "Nested JSON API: identifiers/offer objects, specs as name/value/unit lists, GHz clocks.",
                 "EUR", Decimal(1), False, {"feed.json": "utf-8"}, "json", None, ALL_COLUMNS,
                 lambda n, pid: _hash_sku("", n, pid, 10).lower(), _render_json),
    ]
}  # fmt: skip
