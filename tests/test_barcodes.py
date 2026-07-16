from suparch.barcodes import canonicalize_gtin


def test_canonicalizes_valid_upc_and_gtin14() -> None:
    assert canonicalize_gtin("012345678905") == "00012345678905"
    assert canonicalize_gtin("00012345678905") == "00012345678905"


def test_rejects_invalid_gtin_length_and_check_digit() -> None:
    assert canonicalize_gtin("1234567") is None
    assert canonicalize_gtin("012345678904") is None
