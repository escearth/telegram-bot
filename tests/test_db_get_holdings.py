import pytest
import sqlite3
import json
import threading
from bot import db_get_holdings

def test_db_get_holdings_existing(monkeypatch, tmp_path):
    # Setup temporary database
    db_file = tmp_path / "test_bot_data.db"
    monkeypatch.setattr("bot.DB_FILE", str(db_file))

    # Initialize test database
    conn = sqlite3.connect(str(db_file))
    c = conn.cursor()
    c.execute("""
        CREATE TABLE holdings (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    test_data = {"BTC": 1.5, "ETH": 10.0}
    c.execute("INSERT INTO holdings (user_id, data) VALUES (?, ?)", (123, json.dumps(test_data)))
    conn.commit()
    conn.close()

    # Need to mock the db_lock just in case although it's a global lock instance in bot
    monkeypatch.setattr("bot.db_lock", threading.Lock())

    result = db_get_holdings(123)
    assert result == test_data
    assert isinstance(result, dict)
    assert result["BTC"] == 1.5
    assert result["ETH"] == 10.0

def test_db_get_holdings_none(monkeypatch, tmp_path):
    # Setup temporary database
    db_file = tmp_path / "test_bot_data.db"
    monkeypatch.setattr("bot.DB_FILE", str(db_file))

    # Initialize test database
    conn = sqlite3.connect(str(db_file))
    c = conn.cursor()
    c.execute("""
        CREATE TABLE holdings (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr("bot.db_lock", threading.Lock())

    result = db_get_holdings(456)
    assert result is None
