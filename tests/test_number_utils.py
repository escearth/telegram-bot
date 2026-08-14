from decimal import Decimal
from number_utils import format_wallet_balance

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
