import unittest
from decimal import Decimal
from number_utils import format_fiat

class TestFormatFiat(unittest.TestCase):

    def test_format_fiat_default_decimals(self):
        # Default is 2 decimals

        # Standard case with rounding down
        self.assertEqual(format_fiat(Decimal('1234.567')), '1,234.56')

        # Rounding down edge cases
        self.assertEqual(format_fiat(Decimal('1234.569')), '1,234.56')
        self.assertEqual(format_fiat(Decimal('1234.560')), '1,234.56')

        # `.00` stripping rule
        self.assertEqual(format_fiat(Decimal('1234.00')), '1,234')
        self.assertEqual(format_fiat(Decimal('1234')), '1,234')
        self.assertEqual(format_fiat(Decimal('1234.001')), '1,234') # rounded down to 1234.00, then stripped

        # Small values
        self.assertEqual(format_fiat(Decimal('0.56')), '0.56')
        self.assertEqual(format_fiat(Decimal('0.00')), '0') # .00 stripped

    def test_format_fiat_zero_decimals(self):
        # Decimals = 0 (e.g. for Toman)

        self.assertEqual(format_fiat(Decimal('1234.56'), decimals=0), '1,234')
        self.assertEqual(format_fiat(Decimal('1234.99'), decimals=0), '1,234')
        self.assertEqual(format_fiat(Decimal('1234.00'), decimals=0), '1,234')
        self.assertEqual(format_fiat(Decimal('1234567'), decimals=0), '1,234,567')
        self.assertEqual(format_fiat(Decimal('0'), decimals=0), '0')

    def test_format_fiat_custom_decimals(self):
        # Custom decimals, making sure `.00` stripping doesn't apply to `.000` etc

        # 3 decimals
        self.assertEqual(format_fiat(Decimal('1234.5678'), decimals=3), '1,234.567')
        self.assertEqual(format_fiat(Decimal('1234.000'), decimals=3), '1,234.000') # Not stripped

        # 1 decimal
        self.assertEqual(format_fiat(Decimal('1234.56'), decimals=1), '1,234.5')
        self.assertEqual(format_fiat(Decimal('1234.0'), decimals=1), '1,234.0') # Not stripped

    def test_format_fiat_edge_cases(self):
        # Zero
        self.assertEqual(format_fiat(Decimal('0')), '0') # .00 stripped
        self.assertEqual(format_fiat(Decimal('0'), decimals=3), '0.000')

        # Negative values
        self.assertEqual(format_fiat(Decimal('-1234.56')), '-1,234.56')
        self.assertEqual(format_fiat(Decimal('-1234.00')), '-1,234')
        self.assertEqual(format_fiat(Decimal('-1234.56'), decimals=0), '-1,234')

        # Large numbers
        self.assertEqual(format_fiat(Decimal('1234567890.12')), '1,234,567,890.12')
        self.assertEqual(format_fiat(Decimal('-1234567890.12')), '-1,234,567,890.12')
        self.assertEqual(format_fiat(Decimal('1234567890.1234'), decimals=4), '1,234,567,890.1234')

if __name__ == '__main__':
    unittest.main()
