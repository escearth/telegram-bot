import os
import sqlite3
import pytest

# Dummy token to allow bot import
os.environ['TELEGRAM_BOT_TOKEN'] = '12345:dummy'
import bot

def test_db_add_alert_success(monkeypatch, tmp_path):
    """
    Test the bot.db_add_alert function.
    It should insert an alert correctly and return the new alert_id.
    """
    # Setup test db
    db_file = tmp_path / "test_db.sqlite"
    monkeypatch.setattr(bot, "DB_FILE", str(db_file))

    # Initialize db schema
    bot.init_db()

    # Mock time
    MOCK_TIME = 1620000000.0
    monkeypatch.setattr("time.time", lambda: MOCK_TIME)

    user_id = 123
    crypto_id = "bitcoin"
    symbol = "btc"
    target_price = 50000.0
    direction = "up"

    # Add alert
    alert_id = bot.db_add_alert(user_id, crypto_id, symbol, target_price, direction)
    assert isinstance(alert_id, int)

    # Check it was actually added
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, crypto_id, symbol, target_price, direction, created_at FROM alerts WHERE id=?", (alert_id,))
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == user_id
    assert row[1] == crypto_id
    assert row[2] == "BTC"  # Should be uppercase
    assert row[3] == target_price
    assert row[4] == direction
    assert row[5] == MOCK_TIME
