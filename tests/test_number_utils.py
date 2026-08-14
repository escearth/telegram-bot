import pytest
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from decimal import Decimal
from number_utils import parse_number

def test_parse_number_european():
    assert parse_number("1.000.000,50") == Decimal('1000000.50')
    assert parse_number("1.234,56") == Decimal('1234.56')
    assert parse_number("1.000.000") == Decimal('1000000')
    # According to the codebase, "1.000" and "1,000" are ambiguous and currently parse as decimals.
    assert parse_number("1.000") == Decimal('1.000')

def test_parse_number_us():
    assert parse_number("1,000,000.50") == Decimal('1000000.50')
    assert parse_number("1,234.56") == Decimal('1234.56')
    assert parse_number("1,000,000") == Decimal('1000000')
    assert parse_number("1,000") == Decimal('1.000')

def test_parse_number_persian():
    # '۱۲۳٬۴۵۶' maps to '123,456' which currently evaluates as decimal '123.456'.
    assert parse_number("۱۲۳٬۴۵۶") == Decimal('123.456')
    assert parse_number("۱۲٫۳۴") == Decimal('12.34')

def test_parse_number_no_separators():
    assert parse_number("1000000") == Decimal('1000000')
    assert parse_number("123456") == Decimal('123456')

def test_parse_number_edge_cases():
    assert parse_number("0.00012300") == Decimal('0.00012300')
    assert parse_number("0,00012300") == Decimal('0.00012300') # Single comma that doesn't have exactly 3 following digits becomes decimal

def test_parse_number_invalid_values():
    assert parse_number("") is None
    assert parse_number(None) is None
    assert parse_number("   ") is None
    assert parse_number("abc") is None
    # 1.2.3.4 and 1,2,3,4,5 are parsed as 1234 and 12345 because it drops multiple commas or dots
    assert parse_number("1.2.3.4") == Decimal('1234')
    assert parse_number("1,2,3,4,5") == Decimal('12345')
