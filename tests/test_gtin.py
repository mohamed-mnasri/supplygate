from supplygate.gtin import SYNTHETIC_PREFIX, check_digit, is_valid, synthetic_gtin


def test_known_real_ean_is_valid():
    assert is_valid("4006381333931")
    assert not is_valid("4006381333932")


def test_rejects_bad_shapes():
    assert not is_valid(None)
    assert not is_valid("123")
    assert not is_valid("400638133393X")


def test_check_digit_zero_case():
    assert check_digit("200000000000") == "8"
    assert check_digit("000000000000") == "0"


def test_synthetic_gtin_is_valid_deterministic_and_internal_prefix():
    g = synthetic_gtin("cpu-amd-r7-7800x3d")
    assert g == synthetic_gtin("cpu-amd-r7-7800x3d")
    assert g.startswith(SYNTHETIC_PREFIX) and len(g) == 13 and is_valid(g)
    assert g != synthetic_gtin("cpu-amd-r5-7600x")
