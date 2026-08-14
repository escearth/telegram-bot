import pytest
import sqlite3
import os
import bot

# We need to set a dummy token for bot.py to not fail initialization
# This ensures it doesn't fail when bot is imported, though we also pass it in the test command
os.environ["TELEGRAM_BOT_TOKEN"] = "12345:dummy"

@pytest.fixture(autouse=True)
def setup_teardown_db(monkeypatch):
    """Setup and teardown a temporary test database for wallet operations."""
    test_db = "test_bot_data_clear_wallets.db"
    # Monkeypatch the DB_FILE in the bot module to use our test DB
    monkeypatch.setattr(bot, 'DB_FILE', test_db)

    # Initialize the database schemas
    bot.init_db()

    yield

    # Clean up the test database after the test
    if os.path.exists(test_db):
        os.remove(test_db)


def test_db_clear_wallets_success():
    """Test clearing all wallets for a user when they have multiple wallets."""
    user_id = 12345
    address1 = "0x1111"
    address2 = "0x2222"

    # Add wallets
    assert bot.db_add_wallet(user_id, address1) is True
    assert bot.db_add_wallet(user_id, address2) is True

    # Verify they were added
    wallets = bot.db_get_wallets(user_id)
    assert len(wallets) == 2
    assert address1 in wallets
    assert address2 in wallets

    # Clear wallets
    assert bot.db_clear_wallets(user_id) is True

    # Verify they were cleared
    wallets_after = bot.db_get_wallets(user_id)
    assert len(wallets_after) == 0


def test_db_clear_wallets_empty():
    """Test clearing wallets when a user has no wallets."""
    user_id = 99999

    # Verify the user has no wallets
    assert len(bot.db_get_wallets(user_id)) == 0

    # Clear wallets
    assert bot.db_clear_wallets(user_id) is False


def test_db_clear_wallets_user_isolation():
    """Test that clearing wallets for one user does not affect another user."""
    user_a = 111
    user_b = 222
    address1 = "0xaaaa"
    address2 = "0xbbbb"

    # Add wallets for both users
    assert bot.db_add_wallet(user_a, address1) is True
    assert bot.db_add_wallet(user_b, address2) is True

    # Clear wallets for user A
    assert bot.db_clear_wallets(user_a) is True

    # Verify user A's wallet is gone
    assert len(bot.db_get_wallets(user_a)) == 0

    # Verify user B's wallet is still there
    wallets_b = bot.db_get_wallets(user_b)
    assert len(wallets_b) == 1
    assert wallets_b[0] == address2
