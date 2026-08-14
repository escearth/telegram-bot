import pytest
import sqlite3
import threading
from bot import db_get_lang, _lang_cache, _lang_cache_lock

def test_db_get_lang(monkeypatch, tmp_path):
    # Setup temporary database
    db_file = tmp_path / "test_bot_data.db"
    monkeypatch.setattr("bot.DB_FILE", str(db_file))

    # Initialize test database
    conn = sqlite3.connect(str(db_file))
    c = conn.cursor()
    c.execute("CREATE TABLE user_languages (user_id INTEGER PRIMARY KEY, lang TEXT)")
    c.execute("INSERT INTO user_languages (user_id, lang) VALUES (123, 'es')")
    conn.commit()
    conn.close()

    # Mock cache variables
    mock_cache = {}
    mock_lock = threading.Lock()
    monkeypatch.setattr("bot._lang_cache", mock_cache)
    monkeypatch.setattr("bot._lang_cache_lock", mock_lock)

    # Test 1: Fetch from database (not in cache)
    assert db_get_lang(123) == 'es'

    # Verify it was added to cache
    assert 123 in mock_cache
    assert mock_cache[123] == 'es'

    # Test 2: Fetch from cache
    mock_cache[123] = 'fr'
    assert db_get_lang(123) == 'fr'

    # Test 3: User not in database defaults to 'en'
    assert db_get_lang(456) == 'en'
    assert mock_cache[456] == 'en'
