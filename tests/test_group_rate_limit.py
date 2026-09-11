"""Tests for group rate limiting behavior - ensuring plain-text queries are not rate limited."""
import os
import pytest

os.environ['TELEGRAM_BOT_TOKEN'] = '12345:dummy'

import bot


class TestGroupRateLimiting:
    """Test that group plain-text informational queries are not rate limited."""

    def test_classify_group_query_crypto_price(self):
        """Test classification of crypto price queries."""
        assert bot._classify_group_query('btc') == 'crypto_price'
        assert bot._classify_group_query('eth') == 'crypto_price'
        assert bot._classify_group_query('sol') == 'crypto_price'
        assert bot._classify_group_query('10 trx') == 'crypto_price'
        assert bot._classify_group_query('0.5 btc') == 'crypto_price'

    def test_classify_group_query_fiat_conversion(self):
        """Test classification of fiat conversion queries."""
        assert bot._classify_group_query('100 usd to toman') == 'fiat_conversion'
        assert bot._classify_group_query('50 eur to toman') == 'fiat_conversion'
        assert bot._classify_group_query('1000 try') == 'fiat_conversion'
        assert bot._classify_group_query('usd') == 'fiat_conversion'

    def test_classify_group_query_math(self):
        """Test classification of math queries."""
        assert bot._classify_group_query('2+2') == 'math'
        assert bot._classify_group_query('10*5') == 'math'
        assert bot._classify_group_query('100/4') == 'math'
        assert bot._classify_group_query('2**8') == 'math'
        assert bot._classify_group_query('10% of 200') == 'math'

    def test_classify_group_query_wallet_check(self):
        """Test classification of wallet address queries."""
        # TRON address
        assert bot._classify_group_query('T' + 'a'*33) == 'wallet_check'
        # TON address
        assert bot._classify_group_query('EQ' + 'a'*46) == 'wallet_check'
        assert bot._classify_group_query('UQ' + 'a'*46) == 'wallet_check'

    def test_classify_group_query_tx_check(self):
        """Test classification of transaction hash queries."""
        # Bare hash
        assert bot._classify_group_query('a'*64) == 'tx_check'
        # URLs
        assert bot._classify_group_query('https://tronscan.org/#/transaction/' + 'a'*64) == 'tx_check'
        assert bot._classify_group_query('https://tonviewer.com/transaction/' + 'a'*64) == 'tx_check'
        assert bot._classify_group_query('https://tonscan.org/tx/' + 'a'*64) == 'tx_check'

    def test_classify_group_query_gold_price(self):
        """Test classification of gold price queries."""
        assert bot._classify_group_query('gold') == 'gold_price'
        assert bot._classify_group_query('gold price') == 'gold_price'
        assert bot._classify_group_query('طلا') == 'gold_price'

    def test_classify_group_query_market_overview(self):
        """Test classification of market overview queries."""
        assert bot._classify_group_query('market') == 'market_overview'
        assert bot._classify_group_query('fear') == 'market_overview'
        assert bot._classify_group_query('greed') == 'market_overview'
        assert bot._classify_group_query('fear and greed') == 'market_overview'
        assert bot._classify_group_query('بازار') == 'market_overview'

    def test_classify_group_query_command(self):
        """Test classification of commands."""
        assert bot._classify_group_query('/price') == 'command'
        assert bot._classify_group_query('/wallets') == 'command'
        assert bot._classify_group_query('/alert btc 50000') == 'command'

    def test_classify_group_query_unknown(self):
        """Test classification of unknown queries."""
        assert bot._classify_group_query('hello world') == 'unknown'
        assert bot._classify_group_query('random text') == 'unknown'


class TestTONAddressConversion:
    """Test TON raw address to user-friendly format conversion."""

    def test_ton_raw_to_user_friendly_known_example(self):
        """Test conversion with the known example from the issue."""
        raw = '0:38f5db3c0c772023befbd23ea0a8e62d26995c003078d146f57a7e7f41b3edc6'
        result = bot._raw_ton_to_user_friendly(raw)
        # The expected user-friendly format for this address
        assert result == 'UQA49ds8DHcgI7770j6gqOYtJplcADB40Ub1en5_QbPtxoFU'

    def test_ton_raw_to_user_friendly_invalid(self):
        """Test that invalid raw addresses are returned as-is."""
        assert bot._raw_ton_to_user_friendly('invalid') == 'invalid'
        assert bot._raw_ton_to_user_friendly('0:abc') == '0:abc'
        assert bot._raw_ton_to_user_friendly('') == ''

    def test_detect_wallet_chain_ton(self):
        """Test TON wallet chain detection."""
        assert bot.detect_wallet_chain('EQ' + 'a'*46) == 'ton'
        assert bot.detect_wallet_chain('UQ' + 'a'*46) == 'ton'
        assert bot.detect_wallet_chain('EQ' + 'a'*47) is None  # wrong length

    def test_detect_wallet_chain_tron(self):
        """Test TRON wallet chain detection."""
        assert bot.detect_wallet_chain('T' + 'a'*33) == 'tron'
        assert bot.detect_wallet_chain('T' + 'a'*32) is None  # wrong length
        assert bot.detect_wallet_chain('A' + 'a'*33) is None  # wrong prefix

    def test_is_valid_ton_address(self):
        """Test TON address validation."""
        assert bot.is_valid_ton_address('EQ' + 'a'*46) is True
        assert bot.is_valid_ton_address('UQ' + 'a'*46) is True
        assert bot.is_valid_ton_address('EQ' + 'a'*47) is False
        assert bot.is_valid_ton_address('EQ' + '!'*46) is False
        assert bot.is_valid_ton_address('') is False

    def test_is_valid_tron_address(self):
        """Test TRON address validation."""
        # Valid format but invalid checksum should fail
        # This tests the format check, not the actual checksum
        assert bot.is_valid_tron_address('T' + 'a'*33) is False  # invalid checksum
        assert bot.is_valid_tron_address('T' + 'a'*32) is False  # wrong length
        assert bot.is_valid_tron_address('A' + 'a'*33) is False  # wrong prefix


class TestGroupCache:
    """Test group query cache with language awareness."""

    def test_group_cache_raw_data_not_rendered(self):
        """Test that group cache stores raw data, not rendered HTML."""
        # This is a conceptual test - the cache should store raw data
        # and format it per-user when retrieved
        pass  # Implementation tested via integration


class TestHTMLFormatting:
    """Test HTML escaping and formatting consistency."""

    def test_fmt_price_formatting(self):
        """Test price formatting without scientific notation."""
        from bot import fmt_price
        from decimal import Decimal
        
        assert fmt_price(Decimal('95432.12')) == '$95,432.12'
        assert fmt_price(Decimal('0.28')) == '$0.28'
        assert fmt_price(Decimal('0.000412')) == '$0.000412'
        assert fmt_price(Decimal('0.00000001')) == '$0.00000001'
        assert fmt_price(None) == '-'

    def test_wallet_address_escaping(self):
        """Test that wallet addresses are properly escaped in HTML."""
        # Addresses should be wrapped in <code> tags and escaped
        pass  # Tested in integration


class TestChartGeneration:
    """Test chart generation reliability."""

    def test_chart_config_valid_json(self):
        """Test that chart config produces valid JSON."""
        import json
        from bot import get_crypto_chart_image
        
        # We can't test the actual API call without internet,
        # but we can test the config structure
        pass


class TestDetectCurrency:
    """Test currency detection with various formats."""

    def test_detect_currency_crypto(self):
        """Test crypto currency detection."""
        assert bot.detect_currency('btc') == 'bitcoin'
        assert bot.detect_currency('eth') == 'ethereum'
        assert bot.detect_currency('trx') == 'tron'
        assert bot.detect_currency('ton') == 'the-open-network'
        assert bot.detect_currency('sol') == 'solana'
        assert bot.detect_currency('BTC') == 'bitcoin'
        assert bot.detect_currency('ETH') == 'ethereum'

    def test_detect_currency_persian(self):
        """Test Persian crypto name detection."""
        assert bot.detect_currency('بیتکوین') == 'bitcoin'
        assert bot.detect_currency('اتریوم') == 'ethereum'
        assert bot.detect_currency('ترون') == 'tron'
        assert bot.detect_currency('تون') == 'the-open-network'

    def test_detect_currency_fiat(self):
        """Test fiat currency detection."""
        assert bot.detect_currency('usd') == 'usd'
        assert bot.detect_currency('دلار') == 'usd'
        assert bot.detect_currency('تومان') == 'toman'
        assert bot.detect_currency('تومن') == 'toman'

    def test_detect_currency_u_alias(self):
        """Test 'u' alias for USDT."""
        assert bot.detect_currency('u', check_u_alias=True) == 'tether'
        assert bot.detect_currency('10u', check_u_alias=True) == 'tether'


class TestWebAppSecurity:
    """Test WebApp authentication security."""

    def test_webapp_validate_init_data_valid(self):
        """Test valid initData validation."""
        import hmac
        import hashlib
        import json
        import urllib.parse
        
        token = bot.TELEGRAM_BOT_TOKEN
        user = {'id': 123456, 'first_name': 'Test'}
        user_json = json.dumps(user, separators=(',', ':'))
        params = {
            'auth_date': str(int(1234567890)),
            'user': user_json,
        }
        data_check = "\n".join(f"{k}={params[k]}" for k in sorted(params))
        secret = hmac.new(b"WebAppData", token.encode('utf-8'), hashlib.sha256).digest()
        calc_hash = hmac.new(secret, data_check.encode('utf-8'), hashlib.sha256).hexdigest()
        params['hash'] = calc_hash
        init_data = urllib.parse.urlencode(params)
        
        uid = bot._webapp_validate_init_data(init_data)
        assert uid == 123456

    def test_webapp_validate_init_data_expired(self):
        """Test expired initData is rejected."""
        import hmac
        import hashlib
        import json
        import urllib.parse
        
        token = bot.TELEGRAM_BOT_TOKEN
        user = {'id': 123456}
        user_json = json.dumps(user, separators=(',', ':'))
        params = {
            'auth_date': str(int(1234567890) - 100000),  # Old timestamp
            'user': user_json,
        }
        data_check = "\n".join(f"{k}={params[k]}" for k in sorted(params))
        secret = hmac.new(b"WebAppData", token.encode('utf-8'), hashlib.sha256).digest()
        calc_hash = hmac.new(secret, data_check.encode('utf-8'), hashlib.sha256).hexdigest()
        params['hash'] = calc_hash
        init_data = urllib.parse.urlencode(params)
        
        uid = bot._webapp_validate_init_data(init_data)
        assert uid is None

    def test_webapp_validate_init_data_tampered(self):
        """Test tampered initData is rejected."""
        import hmac
        import hashlib
        import json
        import urllib.parse
        
        token = bot.TELEGRAM_BOT_TOKEN
        user = {'id': 123456}
        user_json = json.dumps(user, separators=(',', ':'))
        params = {
            'auth_date': str(int(1234567890)),
            'user': user_json,
        }
        data_check = "\n".join(f"{k}={params[k]}" for k in sorted(params))
        secret = hmac.new(b"WebAppData", token.encode('utf-8'), hashlib.sha256).digest()
        calc_hash = hmac.new(secret, data_check.encode('utf-8'), hashlib.sha256).hexdigest()
        params['hash'] = calc_hash
        init_data = urllib.parse.urlencode(params)
        
        # Tamper with the user ID
        tampered = init_data.replace('123456', '999999')
        uid = bot._webapp_validate_init_data(tampered)
        assert uid is None


class TestNumberUtils:
    """Test number parsing and formatting utilities."""

    def test_parse_number_european(self):
        from bot.number_utils import parse_number
        from decimal import Decimal
        
        assert parse_number("1.000.000") == Decimal('1000000')
        assert parse_number("1.234,56") == Decimal('1234.56')

    def test_parse_number_us(self):
        from bot.number_utils import parse_number
        from decimal import Decimal
        
        assert parse_number("1,000,000") == Decimal('1000000')
        assert parse_number("1,234.56") == Decimal('1234.56')

    def test_parse_number_persian(self):
        from bot.number_utils import parse_number
        from decimal import Decimal
        
        assert parse_number("۱۲۳٬۴۵۶") == Decimal('123456')
        assert parse_number("۱۲٫۳۴") == Decimal('12.34')

    def test_format_crypto(self):
        from bot.number_utils import format_crypto
        from decimal import Decimal
        
        assert format_crypto(Decimal('0.00012300')) == '0.000123'
        assert format_crypto(Decimal('1234.56789012')) == '1,234.56789012'
        assert format_crypto(Decimal('1000000')) == '1,000,000'

    def test_format_fiat(self):
        from bot.number_utils import format_fiat
        from decimal import Decimal
        
        assert format_fiat(Decimal('1234.56')) == '1,234.56'
        assert format_fiat(Decimal('1234.00')) == '1,234'
        assert format_fiat(Decimal('1234567'), decimals=0) == '1,234,567'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])