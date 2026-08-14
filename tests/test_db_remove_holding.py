import os
import sqlite3
import pytest
from unittest.mock import MagicMock

os.environ['TELEGRAM_BOT_TOKEN'] = '12345:dummy'

import bot

@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    db_file = tmp_path / "test_bot_data.db"
    monkeypatch.setattr(bot, "DB_FILE", str(db_file))

    # Initialize schema
    bot.init_db()

    monkeypatch.setattr(bot, "cache_get", MagicMock(return_value=None))
    monkeypatch.setattr(bot, "cache_set", MagicMock())

    yield

def test_db_remove_holding_no_holdings():
    assert bot.db_remove_holding(1, "BTC") is False

def test_db_remove_holding_symbol_not_found():
    bot.db_set_holdings(1, {"ETH": 1.5, "ADA": 100})
    assert bot.db_remove_holding(1, "BTC") is False
    assert bot.db_get_holdings(1) == {"ETH": 1.5, "ADA": 100}

def test_db_remove_holding_symbol_found_others_remain():
    bot.db_set_holdings(1, {"BTC": 0.5, "ETH": 1.5})
    assert bot.db_remove_holding(1, "btc") is True
    assert bot.db_get_holdings(1) == {"ETH": 1.5}

def test_db_remove_holding_symbol_found_none_remain():
    bot.db_set_holdings(1, {"BTC": 0.5})
    assert bot.db_remove_holding(1, "BTC") is True
    assert bot.db_get_holdings(1) is None

    conn = sqlite3.connect(bot.DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM holdings WHERE user_id=?", (1,))
    count = c.fetchone()[0]
    conn.close()
    assert count == 0
