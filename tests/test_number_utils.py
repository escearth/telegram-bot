import pytest
from number_utils import format_for_locale

def test_format_for_locale_en_default():
    assert format_for_locale('123') == '123'
    assert format_for_locale('1,234.56', 'en') == '1,234.56'

def test_format_for_locale_fa():
    assert format_for_locale('1234567890', 'fa') == '۱۲۳۴۵۶۷۸۹۰'
    assert format_for_locale('1,234.56', 'fa') == '۱,۲۳۴.۵۶'

def test_format_for_locale_mixed_text():
    assert format_for_locale('Hello 123 World', 'fa') == 'Hello ۱۲۳ World'
    assert format_for_locale('Score: 100!', 'fa') == 'Score: ۱۰۰!'

def test_format_for_locale_empty_string():
    assert format_for_locale('', 'fa') == ''
    assert format_for_locale('', 'en') == ''

def test_format_for_locale_no_digits():
    assert format_for_locale('ABC', 'fa') == 'ABC'
    assert format_for_locale('XYZ', 'en') == 'XYZ'
