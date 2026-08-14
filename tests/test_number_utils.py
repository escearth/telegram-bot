import pytest
from decimal import Decimal
from number_utils import format_wallet_balance, format_crypto

def test_format_wallet_balance():
    # Test scenario 1: Happy path with English language setting (default behavior)
    result_en = format_wallet_balance(
        crypto_amount=Decimal('0.5'),
        crypto_symbol='TRX',
        usd_rate=Decimal('0.28'),
        toman_rate=Decimal('60000'),
        user_lang='en'
    )
    assert result_en == '🪙 0.5 TRX\n💵 $0.14\n💰 8,400 Toman'

    # Test scenario 2: Happy path with Persian ('fa') language setting
    result_fa = format_wallet_balance(
        crypto_amount=Decimal('0.5'),
        crypto_symbol='TRX',
        usd_rate=Decimal('0.28'),
        toman_rate=Decimal('60000'),
        user_lang='fa'
    )
    assert result_fa == '🪙 0.5 TRX\n💵 $0.14\n💰 8,400 تومان'

    # Test scenario 3: Zero amount
    result_zero = format_wallet_balance(
        crypto_amount=Decimal('0'),
        crypto_symbol='TRX',
        usd_rate=Decimal('0.28'),
        toman_rate=Decimal('60000'),
        user_lang='en'
    )
    assert result_zero == '🪙 0 TRX\n💵 $0\n💰 0 Toman'

    # Test scenario 4: Large amount with many decimals
    result_large = format_wallet_balance(
        crypto_amount=Decimal('1234.56789012'),
        crypto_symbol='TRX',
        usd_rate=Decimal('1.5'),
        toman_rate=Decimal('50000'),
        user_lang='en'
    )
    assert result_large == '🪙 1,234.56789012 TRX\n💵 $1,851.85\n💰 92,592,591 Toman'

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
