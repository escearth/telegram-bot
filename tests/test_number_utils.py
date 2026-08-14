import unittest
from decimal import Decimal

from number_utils import format_crypto

class TestFormatCrypto(unittest.TestCase):
    def test_zero_values(self):
        """Test zero values."""
        self.assertEqual(format_crypto(Decimal('0')), '0')
        self.assertEqual(format_crypto(Decimal('0.00')), '0')

    def test_trailing_zeros(self):
        """Test small positive decimals to verify trailing zeros are stripped."""
        self.assertEqual(format_crypto(Decimal('0.00012300')), '0.000123')
        self.assertEqual(format_crypto(Decimal('123.4500')), '123.45')
        self.assertEqual(format_crypto(Decimal('123.00')), '123')

    def test_rounding_down(self):
        """Test decimals exceeding max_decimals to verify ROUND_DOWN logic."""
        self.assertEqual(format_crypto(Decimal('1234.567890129')), '1,234.56789012')
        self.assertEqual(format_crypto(Decimal('0.123456789')), '0.12345678')

    def test_thousands_separator(self):
        """Test integer values to verify thousands separator handling."""
        self.assertEqual(format_crypto(Decimal('1000000')), '1,000,000')
        self.assertEqual(format_crypto(Decimal('1234567890')), '1,234,567,890')
        self.assertEqual(format_crypto(Decimal('1234')), '1,234')

    def test_custom_max_decimals(self):
        """Test values with custom max_decimals set."""
        self.assertEqual(format_crypto(Decimal('1234.567'), max_decimals=2), '1,234.56')
        self.assertEqual(format_crypto(Decimal('1234.567'), max_decimals=0), '1,234')
        self.assertEqual(format_crypto(Decimal('0.123456789'), max_decimals=5), '0.12345')

    def test_scientific_notation(self):
        """Test very small values that could trigger scientific notation."""
        self.assertEqual(format_crypto(Decimal('1e-7')), '0.0000001')
        self.assertEqual(format_crypto(Decimal('1.23e-5')), '0.0000123')

    def test_negative_numbers(self):
        """Test negative numbers to ensure proper string formatting behavior."""
        self.assertEqual(format_crypto(Decimal('-1234.56')), '-1,234.56')
        self.assertEqual(format_crypto(Decimal('-0.00012300')), '-0.000123')
        self.assertEqual(format_crypto(Decimal('-1000000')), '-1,000,000')

if __name__ == '__main__':
    unittest.main()
