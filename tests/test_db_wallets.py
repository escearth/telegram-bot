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
    test_db = "test_bot_data_wallets.db"
    # Monkeypatch the DB_FILE in the bot module to use our test DB
    monkeypatch.setattr(bot, 'DB_FILE', test_db)

    # Initialize the database schemas
    bot.init_db()

    yield

    # Clean up the test database after the test
    if os.path.exists(test_db):
        os.remove(test_db)

def test_db_remove_wallet_success():
    """Test successfully removing an existing wallet."""
    user_id = 12345
    address = "0x1234567890abcdef"

    # Add a wallet first
    assert bot.db_add_wallet(user_id, address) is True

    # Remove the wallet and ensure it returns True
    assert bot.db_remove_wallet(user_id, address) is True

    # Verify it is actually removed by trying to remove it again
    assert bot.db_remove_wallet(user_id, address) is False

def test_db_remove_wallet_non_existent():
    """Test removing a wallet that does not exist."""
    user_id = 12345
    address = "0xdeadbeef"

    # Attempting to remove a non-existent wallet should return False
    assert bot.db_remove_wallet(user_id, address) is False

def test_db_remove_wallet_user_isolation():
    """Test that removing a wallet only affects the specified user."""
    user_a = 111
    user_b = 222
    address = "0xsharedaddress"

    # Both users add the same address (this is allowed by the schema: PRIMARY KEY (user_id, address))
    assert bot.db_add_wallet(user_a, address) is True
    assert bot.db_add_wallet(user_b, address) is True

    # User A removes their wallet
    assert bot.db_remove_wallet(user_a, address) is True

    # User B's wallet should still exist, so removing it now should return True
    assert bot.db_remove_wallet(user_b, address) is True

def test_db_remove_wallet_case_sensitivity():
    """Test that wallet addresses are treated as case-sensitive or exactly as stored."""
    user_id = 12345
    address_lower = "0xabcdef"
    address_upper = "0xABCDEF"

    bot.db_add_wallet(user_id, address_lower)

    # Attempting to remove with different casing should fail if exactly matched
    # Since SQLite is case-sensitive by default for TEXT unless COLLATE NOCASE is specified
    assert bot.db_remove_wallet(user_id, address_upper) is False

    # Removing with exact case should succeed
    assert bot.db_remove_wallet(user_id, address_lower) is True
