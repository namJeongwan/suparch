def canonicalize_gtin(value: object) -> str | None:
    if value is None:
        return None
    digits = "".join(character for character in str(value) if character.isdigit())
    if len(digits) not in {8, 12, 13, 14}:
        return None
    body, check_digit = digits[:-1], int(digits[-1])
    weighted_sum = sum(
        int(digit) * (3 if index % 2 == 0 else 1)
        for index, digit in enumerate(reversed(body))
    )
    expected = (10 - weighted_sum % 10) % 10
    if check_digit != expected:
        return None
    return digits.zfill(14)
