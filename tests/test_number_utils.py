import unittest
from decimal import Decimal
from number_utils import parse_number

class TestNumberUtilsParseErrorPaths(unittest.TestCase):
    def test_parse_number_unparseable_strings(self):
        """Test that unparseable text triggers exceptions and returns None."""
        invalid_inputs = [
            "abc",
            "123abc",
            "!@#",
            "invalid number",
            "$100",
            " ",
            "",
            "1,2.3.4",
            "1.2,3,4"
        ]

        for text in invalid_inputs:
            with self.subTest(text=text):
                self.assertIsNone(parse_number(text))

    def test_parse_number_none_object(self):
        """Test that passing None object is handled gracefully or returns None."""
        # Note: str(None) -> "None" which is caught by the exception block
        self.assertIsNone(parse_number(None))

if __name__ == '__main__':
    unittest.main()
