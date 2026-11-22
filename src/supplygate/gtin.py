"""GTIN-13 (EAN-13) helpers.

Synthetic GTINs use the GS1 prefix range 200-299 ("restricted circulation /
internal use"), so they can never collide with a real retail barcode.
"""

from __future__ import annotations

import hashlib

SYNTHETIC_PREFIX = "200"


def check_digit(first12: str) -> str:
    """EAN-13 check digit for a 12-digit body (weights 1,3,1,3... from the left)."""
    if len(first12) != 12 or not first12.isdigit():
        raise ValueError("expected 12 digits")
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return str((10 - total % 10) % 10)


def is_valid(gtin: str | None) -> bool:
    """True if ``gtin`` is 13 digits with a correct check digit."""
    return gtin is not None and len(gtin) == 13 and gtin.isdigit() and check_digit(gtin[:12]) == gtin[12]


def synthetic_gtin(key: str) -> str:
    """Deterministic synthetic GTIN-13 for ``key`` (prefix 200, valid check digit)."""
    body = int(hashlib.sha256(key.encode()).hexdigest(), 16) % 10**9
    first12 = f"{SYNTHETIC_PREFIX}{body:09d}"
    return first12 + check_digit(first12)
