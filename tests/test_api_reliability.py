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