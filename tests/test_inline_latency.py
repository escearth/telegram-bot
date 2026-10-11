"""Regression tests: inline queries must be answered from cache, instantly."""

import os
import time

os.environ['TELEGRAM_BOT_TOKEN'] = '123456:dummy_token_for_testing'

import bot


def _mk_inline(query, n=0):
    return bot.types.InlineQuery(
        id=f"lat{n}",
        from_user=bot.types.User(id=123, is_bot=False, first_name="T"),
        query=query,
        offset="",
    )


def _seed_cache():
    bot.cache_set('usd_to_irr', 100000, ttl=600)
    bot.cache_set('try_irr', 3000, ttl=600)
    bot.cache_set('eur_irr', 110000, ttl=600)
    bot.cache_set('gbp_irr', 130000, ttl=600)
    bot.cache_set('aed_irr', 27000, ttl=600)
    bot.cache_set('cny_irr', 14000, ttl=600)
    bot.cache_set('bitcoin', 50000.0, ttl=600)


def test_cached_lookups_are_not_throttled():
    """A cache hit makes no network call, so it must not sleep for API_COOLDOWN."""
    bot.cache_set('usd_to_irr', 100000, ttl=600)
    start = time.time()
    for _ in range(5):
        assert bot.get_usd_to_irr() == 100000
    assert time.time() - start < 1.0


def test_uncached_calls_are_still_throttled(monkeypatch):
    calls = []

    @bot.rate_limited_api_call(cache_key='_nothing_cached')
    def fn():
        calls.append(time.time())
        return 1

    monkeypatch.setattr(bot, 'API_COOLDOWN', 0.2)
    bot._last_api_call = 0.0
    fn(); fn()
    assert calls[1] - calls[0] >= 0.15


def test_inline_crypto_price_is_fast_and_has_all_fiats(monkeypatch):
    _seed_cache()
    captured = []
    monkeypatch.setattr(bot.bot, 'answer_inline_query',
                        lambda qid, results, **kw: captured.append(results))
    # Any network access from the inline path is a bug.
    def no_network(*a, **kw):
        raise AssertionError("inline path touched the network")
    monkeypatch.setattr(bot.session, 'get', no_network)

    start = time.time()
    bot.inline_query_handler(_mk_inline("btc"))
    assert time.time() - start < 2.0
    assert len(captured) == 1
    text = captured[0][0].input_message_content.message_text
    for code in ('TRY', 'EUR', 'GBP', 'AED', 'CNY'):
        assert code in text


def test_inline_amount_cny_is_converted_not_toman(monkeypatch):
    """CNY line used to multiply USD by Toman-per-CNY (a Toman value)."""
    _seed_cache()
    captured = []
    monkeypatch.setattr(bot.bot, 'answer_inline_query',
                        lambda qid, results, **kw: captured.append(results))
    monkeypatch.setattr(bot.session, 'get',
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("network")))
    bot.inline_query_handler(_mk_inline("1 btc"))
    text = captured[0][0].input_message_content.message_text
    # 50,000 USD * 100,000 / 14,000 = 357,142.86 CNY
    assert '357,142.86 CNY' in text


def test_inline_fiat_to_fiat_uses_cache_only(monkeypatch):
    _seed_cache()
    captured = []
    monkeypatch.setattr(bot.bot, 'answer_inline_query',
                        lambda qid, results, **kw: captured.append(results))
    monkeypatch.setattr(bot.session, 'get',
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("network")))
    start = time.time()
    bot.inline_query_handler(_mk_inline("100 usd to toman"))
    assert time.time() - start < 2.0
    assert captured and captured[0]
