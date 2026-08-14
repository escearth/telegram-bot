import os
import pytest

# Set dummy token before importing bot to avoid initialization errors
os.environ['TELEGRAM_BOT_TOKEN'] = '123456:dummy_token_for_testing'

from bot import evaluate_math

def test_evaluate_math_basic():
    # basic arithmetic
    assert evaluate_math("2 + 2") == "✅ 2 + 2 = 4"
    assert evaluate_math("10 - 5") == "✅ 10 - 5 = 5"
    assert evaluate_math("3 * 4") == "✅ 3 * 4 = 12"
    assert evaluate_math("20 / 4") == "✅ 20 / 4 = 5"

def test_evaluate_math_exponents():
    assert evaluate_math("2^3") == "✅ 2^3 = 8"
    assert evaluate_math("2**3") == "✅ 2**3 = 8"

def test_evaluate_math_percentages():
    # X% of Y
    assert evaluate_math("50% of 200") == "✅ 50% of 200 = 100"
    assert evaluate_math("10 % of 50") == "✅ 10 % of 50 = 5"

    # X + Y%
    assert evaluate_math("100 + 20%") == "✅ 100 + 20% = 120"

    # X - Y%
    assert evaluate_math("100 - 20%") == "✅ 100 - 20% = 80"

def test_evaluate_math_persian():
    # Persian digits are converted in the expr
    assert evaluate_math("۲ + ۲") == "✅ 2 + 2 = 4"
    # Persian percentage "از"
    assert evaluate_math("50% از 200") == "✅ 50% از 200 = 100"

def test_evaluate_math_invalid():
    assert evaluate_math("invalid") == "❌ Invalid expression."
    assert evaluate_math("2 + ") == "❌ Invalid expression."
    assert evaluate_math("1 / 0") == "❌ Invalid expression."
    assert evaluate_math("+") == "❌ Invalid expression."

def test_evaluate_math_large_numbers():
    assert evaluate_math("1000 * 1000") == "✅ 1000 * 1000 = 1,000,000"

def test_evaluate_math_decimal():
    assert evaluate_math("5.5 * 2") == "✅ 5.5 * 2 = 11"

def test_evaluate_math_just_operators():
    assert evaluate_math("()") == "❌ Invalid expression."
    assert evaluate_math("--") == "❌ Invalid expression."
