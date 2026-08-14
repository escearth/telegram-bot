from number_utils import normalize_digits

def test_normalize_digits_persian():
    assert normalize_digits("۰۱۲۳۴۵۶۷۸۹") == "0123456789"

def test_normalize_digits_arabic():
    assert normalize_digits("٠١٢٣٤٥٦٧٨٩") == "0123456789"

def test_normalize_digits_operators():
    assert normalize_digits("٪×÷٬٫") == "%*/,."

def test_normalize_digits_mixed():
    assert normalize_digits("۱,۲۳۴.۵۶٪") == "1,234.56%"
    assert normalize_digits("1000") == "1000"

def test_normalize_digits_empty():
    assert normalize_digits("") == ""
