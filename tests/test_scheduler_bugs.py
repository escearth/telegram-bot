"""Regression tests: alerts fire, digest honours per-user hour, QuickChart URLs encode."""

import os
from datetime import datetime
from unittest.mock import MagicMock

os.environ['TELEGRAM_BOT_TOKEN'] = '123456:dummy_token_for_testing'

import bot


def test_quickchart_helper_url_encodes():
    # `quote` is the blockquote helper; the URL encoder must be separate.
    assert bot.quote("x") == "<blockquote>x</blockquote>"
    assert bot.url_quote('{"a":1}') == '%7B%22a%22%3A1%7D'


def test_alerts_fire_and_are_deleted(monkeypatch):
    alert = {'id': 7, 'user_id': 42, 'crypto_id': 'bitcoin', 'symbol': 'BTC',
             'target_price': 100.0, 'direction': 'above'}
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'bitcoin': {'usd': 150.0}}
    monkeypatch.setattr(bot, 'db_get_all_alerts', lambda: [alert])
    monkeypatch.setattr(bot.session, 'get', lambda *a, **k: resp)
    sent, deleted = [], []
    monkeypatch.setattr(bot.bot, 'send_message', lambda uid, *a, **k: sent.append(uid))
    monkeypatch.setattr(bot, 'db_delete_alert_by_id', lambda i: deleted.append(i))
    bot._check_alerts_once()
    assert sent == [42] and deleted == [7]


def test_alert_not_fired_below_target(monkeypatch):
    alert = {'id': 8, 'user_id': 42, 'crypto_id': 'bitcoin', 'symbol': 'BTC',
             'target_price': 200.0, 'direction': 'above'}
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'bitcoin': {'usd': 150.0}}
    monkeypatch.setattr(bot, 'db_get_all_alerts', lambda: [alert])
    monkeypatch.setattr(bot.session, 'get', lambda *a, **k: resp)
    sent = []
    monkeypatch.setattr(bot.bot, 'send_message', lambda uid, *a, **k: sent.append(uid))
    bot._check_alerts_once()
    assert sent == []


def test_digest_sent_once_at_users_hour(monkeypatch):
    now_h = datetime.now(bot.IRAN_TZ).hour
    monkeypatch.setattr(bot, 'db_get_all_digest_users', lambda: [
        {'user_id': 1, 'hour': now_h},
        {'user_id': 2, 'hour': (now_h + 5) % 24},
    ])
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), **kw):
            self.args = args
        def start(self):
            started.append(self.args[0])

    monkeypatch.setattr(bot.threading, 'Thread', FakeThread)
    bot._digest_last_sent.clear()
    bot._run_digests_once()
    bot._run_digests_once()  # same hour slot: no duplicate
    assert started == [1]


def test_digest_message_builds_without_datetime_utc(monkeypatch):
    monkeypatch.setattr(bot, 'db_get_holdings', lambda uid: {'BTC': 1.0})
    monkeypatch.setattr(bot, 'db_get_buy_prices', lambda uid: {})
    monkeypatch.setattr(bot, 'get_usd_to_irr', lambda: 100000)
    monkeypatch.setattr(bot, '_fetch_prices_batch', lambda ids: {'bitcoin': {'usd_24h_change': 1.5}})
    monkeypatch.setattr(bot, 'get_crypto_price', lambda cid: 50000.0)
    sent = []
    monkeypatch.setattr(bot.bot, 'send_message', lambda uid, text, **k: sent.append(text))
    bot._send_digest_to_user(5)
    assert sent and 'UTC' in sent[0]
