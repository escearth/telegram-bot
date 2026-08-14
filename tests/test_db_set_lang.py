import os
import sqlite3
import pytest

# Dummy token to avoid exit(1) in bot.py
os.environ['TELEGRAM_BOT_TOKEN'] = '12345:dummy'

import bot

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Fixture to provide a temporary database file and clean cache."""
    db_file = tmp_path / "test_bot.db"
    monkeypatch.setattr(bot, "DB_FILE", str(db_file))
    monkeypatch.setattr(bot, "_lang_cache", {})

    # Re-initialize the db on the new temporary file
    bot.init_db()

    return str(db_file)

def test_db_set_lang_updates_db_and_cache(temp_db):
    user_id = 999
    new_lang = 'fa'

    # Call the function
    bot.db_set_lang(user_id, new_lang)

    # 1. Check if the database was updated correctly
    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    c.execute("SELECT lang FROM user_languages WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == new_lang

    # 2. Check if the in-memory cache was updated
    assert bot._lang_cache.get(user_id) == new_lang

def test_db_set_lang_overwrites_existing(temp_db):
    user_id = 888

    # Initial set
    bot.db_set_lang(user_id, 'en')

    # Overwrite
    bot.db_set_lang(user_id, 'fa')

    # Verify DB
    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    c.execute("SELECT lang FROM user_languages WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 'fa'

    # Verify Cache
    assert bot._lang_cache.get(user_id) == 'fa'
