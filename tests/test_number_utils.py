import pytest
from decimal import Decimal
from number_utils import format_crypto

class TestFormatCrypto:
    @pytest.mark.parametrize("value, expected", [
        (Decimal('0'), '0'),
        (Decimal('0.00012300'), '0.000123'),
        (Decimal('1234.56789012'), '1,234.56789012'),
        (Decimal('1000000'), '1,000,000'),
        (Decimal('1.00000000'), '1'),
        (Decimal('1E-7'), '0.0000001'),
        (Decimal('1000000.0001'), '1,000,000.0001'),
        (Decimal('1234.567890129'), '1,234.56789012'),  # rounding down
    ])
    def test_format_crypto_default_decimals(self, value, expected):
        assert format_crypto(value) == expected

    @pytest.mark.parametrize("value, max_decimals, expected", [
        (Decimal('1.12345'), 2, '1.12'),
        (Decimal('1.12999'), 2, '1.12'),  # ROUND_DOWN
        (Decimal('1234.5678'), 0, '1,234'),
        (Decimal('0.000123'), 10, '0.000123'),
    ])
    def test_format_crypto_custom_decimals(self, value, max_decimals, expected):
        assert format_crypto(value, max_decimals=max_decimals) == expected
