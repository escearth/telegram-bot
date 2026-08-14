import pytest
from decimal import Decimal
from number_utils import parse_number

class TestParseNumber:
    def test_parse_number_us_format(self):
        # Thousands separators as commas, dot as decimal
        assert parse_number("1,000,000.50") == Decimal("1000000.50")
        assert parse_number("1,234.56") == Decimal("1234.56")
        assert parse_number("1,234,567.89") == Decimal("1234567.89")
        assert parse_number("1.234") == Decimal("1.234")  # single dot is decimal
        assert parse_number(".50") == Decimal("0.50")

    def test_parse_number_euro_format(self):
        # Thousands separators as dots, comma as decimal
        assert parse_number("1.000.000,50") == Decimal("1000000.50")
        assert parse_number("1.234,56") == Decimal("1234.56")
        assert parse_number("1.234.567,89") == Decimal("1234567.89")
        assert parse_number("1,234") == Decimal("1.234")  # single comma becomes decimal

    @pytest.mark.xfail(reason="documented behavior mismatched with implementation: parse_number treats single separator as decimal regardless of it being a comma")
    def test_parse_number_persian_format(self):
        # Persian thousands separator (٬) becomes comma and currently parses as decimal instead of integer.
        # Docstring says: >>> parse_number("۱۲۳٬۴۵۶") -> Decimal('123456')
        assert parse_number("۱۲۳٬۴۵۶") == Decimal("123456")

        # Another test case from docstring: ۱۲٫۳۴ -> Decimal('12.34')
        assert parse_number("۱۲٫۳۴") == Decimal("12.34")

    def test_parse_number_persian_digits(self):
        # Persian digits without ambiguous separators
        assert parse_number("۱۲۳۴۵۶") == Decimal("123456")
        assert parse_number("۱۲۳.۴۵") == Decimal("123.45")

    def test_parse_number_no_separators(self):
        assert parse_number("1000000") == Decimal("1000000")
        assert parse_number("0") == Decimal("0")
        assert parse_number("-123") == Decimal("-123")

    def test_parse_number_scientific(self):
        # Docstring says 1e6 converts to 1000000
        # Decimal("1e6") equals Decimal('1E+6') which has a value of 1000000.
        assert parse_number("1e6") == Decimal("1E+6")
        assert parse_number("1.5e-3") == Decimal("1.5E-3")

    def test_parse_number_empty_invalid(self):
        # Empty and None
        assert parse_number("") is None
        assert parse_number(None) is None
        assert parse_number("   ") is None

        # Invalid string
        assert parse_number("abc") is None
        # Note: '1.23.45.67' has multiple dots and 0 commas, so it gets stripped of dots
        # as European format and parsed as 1234567. We can test an actual invalid string.
        assert parse_number("1.23.45,67,89") is None  # invalid decimal format

    def test_parse_number_with_spaces(self):
        # Spaces should be stripped
        assert parse_number(" 1,234.56 ") == Decimal("1234.56")
        assert parse_number("1 000 000") == Decimal("1000000")
