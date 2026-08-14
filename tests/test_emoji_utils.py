import pytest
import emoji_utils

def test_apply_emoji_empty_map(monkeypatch):
    # Setup
    monkeypatch.setattr(emoji_utils, '_emoji_map', {})

    # Execution
    text = "Hello 🌍"
    result = emoji_utils.apply_emoji(text)

    # Assertion
    assert result == "Hello 🌍"

def test_apply_emoji_with_map(monkeypatch):
    # Setup
    monkeypatch.setattr(emoji_utils, '_emoji_map', {'🌍': "12345"})

    # Execution
    text = "Hello 🌍"
    result = emoji_utils.apply_emoji(text)

    # Assertion
    assert result == 'Hello <tg-emoji emoji-id="12345">🌍</tg-emoji>'
