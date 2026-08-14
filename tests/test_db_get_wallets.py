import pytest
import sqlite3
import os
import bot

# Fixture to create a temporary database for testing
@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "test_bot_data.db"
    # Override DB_FILE in bot module
    monkeypatch.setattr(bot, "DB_FILE", str(db_file))

    # Init the tables using the real function
    bot.init_db()

    # Return the db path if tests need it directly
    yield str(db_file)

def test_db_get_wallets_empty(temp_db):
    user_id = 123
    wallets = bot.db_get_wallets(user_id)
    assert wallets == []

def test_db_get_wallets_with_data(temp_db):
    user_id = 456
    address1 = "0xABC"
    address2 = "0xDEF"

    # Insert data directly
    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    c.execute("INSERT INTO wallets (user_id, address) VALUES (?,?)", (user_id, address1))
    c.execute("INSERT INTO wallets (user_id, address) VALUES (?,?)", (user_id, address2))
    conn.commit()
    conn.close()

    wallets = bot.db_get_wallets(user_id)
    assert isinstance(wallets, list)
    assert len(wallets) == 2
    assert address1 in wallets
    assert address2 in wallets

def test_db_get_wallets_multiple_users(temp_db):
    user_id1 = 111
    user_id2 = 222

    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    c.execute("INSERT INTO wallets (user_id, address) VALUES (?,?)", (user_id1, "wallet1"))
    c.execute("INSERT INTO wallets (user_id, address) VALUES (?,?)", (user_id2, "wallet2"))
    conn.commit()
    conn.close()

    wallets_user1 = bot.db_get_wallets(user_id1)
    assert wallets_user1 == ["wallet1"]

    wallets_user2 = bot.db_get_wallets(user_id2)
    assert wallets_user2 == ["wallet2"]
