import unittest
from decimal import Decimal
from number_utils import format_fiat

class TestFormatFiat(unittest.TestCase):

    def test_format_fiat_usd_happy_path(self):
        """Test formatting standard amounts with default 2 decimals (USD)"""
        self.assertEqual(format_fiat(Decimal('1234.56')), '1,234.56')
        self.assertEqual(format_fiat(Decimal('0.12')), '0.12')
        self.assertEqual(format_fiat(Decimal('10.50')), '10.50')

    def test_format_fiat_toman_happy_path(self):
        """Test formatting amounts with 0 decimals (Toman)"""
        self.assertEqual(format_fiat(Decimal('60000000'), decimals=0), '60,000,000')
        self.assertEqual(format_fiat(Decimal('1234.56'), decimals=0), '1,234')
        self.assertEqual(format_fiat(Decimal('1234.99'), decimals=0), '1,234')  # Quantize with ROUND_DOWN

    def test_format_fiat_strip_zeros_whole_numbers(self):
        """Test that .00 is stripped for whole numbers when decimals=2"""
        self.assertEqual(format_fiat(Decimal('1234.00')), '1,234')
        self.assertEqual(format_fiat(Decimal('100')), '100')

    def test_format_fiat_keep_zeros_if_not_whole(self):
        """Test that trailing zeros are kept if it's not exactly .00"""
        self.assertEqual(format_fiat(Decimal('1234.50')), '1,234.50')

    def test_format_fiat_different_decimals(self):
        """Test formatting with arbitrary decimal places"""
        self.assertEqual(format_fiat(Decimal('1234.567'), decimals=3), '1,234.567')
        self.assertEqual(format_fiat(Decimal('1234.5'), decimals=3), '1,234.500')
        self.assertEqual(format_fiat(Decimal('1234.000'), decimals=3), '1,234.000') # .000 shouldn't be stripped as it's only .00 for decimals=2

    def test_format_fiat_zero(self):
        """Test formatting zero"""
        self.assertEqual(format_fiat(Decimal('0')), '0')
        self.assertEqual(format_fiat(Decimal('0.00')), '0')
        self.assertEqual(format_fiat(Decimal('0'), decimals=0), '0')
        self.assertEqual(format_fiat(Decimal('0'), decimals=3), '0.000')

    def test_format_fiat_negative_numbers(self):
        """Test formatting negative numbers"""
        self.assertEqual(format_fiat(Decimal('-1234.56')), '-1,234.56')
        self.assertEqual(format_fiat(Decimal('-1234.00')), '-1,234')
        self.assertEqual(format_fiat(Decimal('-60000000'), decimals=0), '-60,000,000')

    def test_format_fiat_large_numbers(self):
        """Test that thousands separators are applied correctly on very large numbers"""
        self.assertEqual(format_fiat(Decimal('1234567890.12')), '1,234,567,890.12')
        self.assertEqual(format_fiat(Decimal('1234567890'), decimals=0), '1,234,567,890')

if __name__ == '__main__':
    unittest.main()
