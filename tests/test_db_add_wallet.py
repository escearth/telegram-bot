import os
import sqlite3
import pytest

# Dummy token to allow bot import
os.environ['TELEGRAM_BOT_TOKEN'] = '12345:dummy'
import bot

def test_db_add_wallet_success_and_duplicate(monkeypatch, tmp_path):
    """
    Test the bot.db_add_wallet function.
    It should return True on the first insertion.
    It should return False on inserting the same (user_id, address)
    due to sqlite3.IntegrityError.
    """
    # Setup test db
    db_file = tmp_path / "test_db.sqlite"
    monkeypatch.setattr(bot, "DB_FILE", str(db_file))

    # Initialize db schema
    bot.init_db()

    user_id = 123
    address = "0xABCDEF"

    # First add should succeed
    success = bot.db_add_wallet(user_id, address)
    assert success is True

    # Check it was actually added
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM wallets WHERE user_id=? AND address=?", (user_id, address))
    row = cursor.fetchone()
    conn.close()
    assert row is not None

    # Second add of the same user_id and address should fail (duplicate)
    success2 = bot.db_add_wallet(user_id, address)
    assert success2 is False
