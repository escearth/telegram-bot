"""Tests for API/thread reliability fixes introduced after the Sept/Oct 2026
log audit: Telegram retry wrapper, chart-source fallbacks and idempotent
startup guards."""

import os

os.environ['TELEGRAM_BOT_TOKEN'] = '123456:dummy_token_for_testing'

import bot


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def test_coincap_interval_always_valid():
    """CoinCap rejects d7/d30 (HTTP 400). Every range must map to a valid interval."""
    valid = {'m1', 'm5', 'm15', 'm30', 'h1', 'h2', 'h6', 'h12', 'd1'}
    for days in (1, 7, 8, 30, 90, 365, 1000):
        assert bot._coincap_interval(days) in valid
    assert bot._coincap_interval(1) == 'h6'
    assert bot._coincap_interval(7) == 'h6'
    assert bot._coincap_interval(30) == 'd1'
    assert bot._coincap_interval(365) == 'd1'


def test_fetch_chart_data_fallback_uses_valid_interval(monkeypatch):
    """The 30-day chart must NOT request the invalid d7/d30 CoinCap interval."""
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen['url'] = url
        seen['interval'] = params.get('interval')
        return _FakeResp({'data': [{'time': 1609459200000, 'priceUsd': '0.02'}]})

    monkeypatch.setattr(bot.session, 'get', fake_get)
    rows = bot._fetch_chart_data_fallback('the-open-network', days=30)
    assert rows == [[1609459200000, 0.02]]
    assert seen['interval'] == 'd1'
    assert '/toncoin/history' in seen['url']


def test_fetch_chart_data_binance_parses_klines(monkeypatch):
    klines = [[1609459200000, '0.019', '0.02', '0.018', '0.0195', '100', 1609459260000, '1.9', 100, '1', '0', '0']]
    monkeypatch.setattr(
        bot.session, 'get',
        lambda *a, **kw: _FakeResp(klines)
    )
    rows = bot._fetch_chart_data_binance('bitcoin', days=30)
    assert rows == [[1609459200000, 0.0195]]


def test_fetch_chart_data_binance_skips_unsupported(monkeypatch):
    """Symbols without a Binance pair (e.g. telegram-stars) are skipped, not fetched."""
    called = []

    def fake_get(*args, **kwargs):
        called.append(args)
        return _FakeResp([])

    monkeypatch.setattr(bot.session, 'get', fake_get)
    assert bot._fetch_chart_data_binance('telegram-stars', days=30) == []
    assert called == []


def test_install_telegram_retry_is_idempotent():
    """Re-importing / double-calling must not stack a second retry wrapper."""
    from telebot import apihelper
    first = apihelper._make_request
    bot._TG_RETRY_INSTALLED = False
    bot._install_telegram_retry()
    second = apihelper._make_request
    # No extra wrapping happened (flag would be present and idempotent-guarded).
    assert second is first
    assert getattr(second, '_tg_retry_wrapped', False) is True
    assert bot._TG_RETRY_INSTALLED is True


def test_telegram_retry_after_parser():
    """Retry-After is read from the ApiTelegramException result_json payload."""
    class FakeErr:
        result_json = {'parameters': {'retry_after': 5}}
    assert bot._telegram_retry_after(FakeErr()) == 5

    class NoParamErr:
        result_json = {}
    assert bot._telegram_retry_after(NoParamErr()) is None


def test_startup_guards_exist():
    """The single-instance guards are registered as module state."""
    assert bot._scheduler_started is False
    assert bot._webapp_started is False
    assert bot._polling_started is False
    assert bot._start_scheduler_once is not None


# ── Inline mode fixes ────────────────────────────────────────────────────

def _mk_inline(query, n=0):
    return bot.types.InlineQuery(
        id=f"q{n}",
        from_user=bot.types.User(id=123, is_bot=False, first_name="T"),
        query=query,
        offset="",
    )


def _capture_answer(monkeypatch):
    captured = []

    def fake_answer(qid, results, **kw):
        captured.append((qid, results, kw))

    monkeypatch.setattr(bot.bot, 'answer_inline_query', fake_answer)
    return captured


def test_inline_empty_query_answers_guide(monkeypatch):
    """First-open (empty) inline queries must still get an answer, not be dropped."""
    captured = _capture_answer(monkeypatch)
    bot.inline_query_handler(_mk_inline(""))
    assert len(captured) == 1
    qid, results, kw = captured[0]
    assert qid == "q0"
    titles = [r.title for r in results]
    assert any('Guide' in t for t in titles)
    assert kw.get('switch_pm_parameter') == 'inline_help'


def test_inline_try_lira_not_duplicated(monkeypatch):
    """TRY/TL/Lira previously produced two identical results (sections 13+13b)."""
    bot.cache_set('try_irr', 9000.0, ttl=60)
    for i, q in enumerate(('try', 'tl', 'lira')):
        captured = _capture_answer(monkeypatch)
        bot.inline_query_handler(_mk_inline(q, i))
        assert len(captured) == 1, q
        results = captured[0][1]
        assert len(results) == 1, (q, [r.title for r in results])
        ids = [r.id for r in results]
        assert len(ids) == len(set(ids))


def test_inline_crashed_handler_still_answers(monkeypatch):
    """If a provider call throws mid-query, we still answer with the help guide."""
    captured = _capture_answer(monkeypatch)

    def boom(inline_query):
        raise RuntimeError("provider exploded")

    bot._safe_inline(boom)(_mk_inline("btc"))
    assert len(captured) == 1
    assert captured[0][0] == "q0"
    titles = [r.title for r in captured[0][1]]
    assert any('Guide' in t for t in titles)


def test_inline_market_uses_cache_first(monkeypatch):
    """Inline 'market' must serve from cache, never hitting the rate-limited helpers."""
    bot.cache_set('cg_global', {
        'total_market_cap': {'usd': 2.2e12},
        'total_volume': {'usd': 1.1e11},
        'market_cap_percentage': {'btc': 55.0, 'eth': 15.0},
        'active_cryptocurrencies': 12345,
        'market_cap_change_percentage_24h_usd': 3.5,
    }, ttl=120)
    bot.cache_set('fng', {'value': 60, 'value_classification': 'Greed'}, ttl=300)
    captured = _capture_answer(monkeypatch)
    bot.inline_query_handler(_mk_inline("market"))
    assert len(captured) == 1
    titles = [r.title for r in captured[0][1]]
    assert any('Market' in t for t in titles)