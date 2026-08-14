import pytest
import emoji_utils

@pytest.fixture
def mock_emoji_map(monkeypatch):
    """Fixture to mock _emoji_map with test data."""
    mock_map = {
        '✅': '12345',
        '📊': '54321',
        # Used for _FLAG_FALLBACK fallback mapping
        '🌍': '99999',
        # Used for _EMOJI_FALLBACK
        '\u26a0\ufe0f': '11111', # ⚠️
        '💰': '22222', # 🪙 fallback
        '📱': '33333',
    }
    monkeypatch.setattr(emoji_utils, '_emoji_map', mock_map)
    return mock_map


def test_apply_emoji_no_map(monkeypatch):
    """Test behavior when _emoji_map is empty."""
    monkeypatch.setattr(emoji_utils, '_emoji_map', {})
    text = "Hello ✅"
    assert emoji_utils.apply_emoji(text) == text


def test_apply_emoji_no_emojis(mock_emoji_map):
    """Test behavior when text contains no emojis."""
    text = "Hello world!"
    assert emoji_utils.apply_emoji(text) == text


def test_apply_emoji_direct_match(mock_emoji_map):
    """Test behavior for emojis directly in the map."""
    text = "Status ✅ completed"
    expected = 'Status <tg-emoji emoji-id="12345">✅</tg-emoji> completed'
    assert emoji_utils.apply_emoji(text) == expected


def test_apply_emoji_skip_emoji(mock_emoji_map):
    """Test behavior for emojis in _EMOJI_SKIP."""
    text = "Orange circle 🟠"
    assert emoji_utils.apply_emoji(text) == text


def test_apply_emoji_flag_fallback(mock_emoji_map):
    """Test behavior for flag fallback."""
    text = "Turkey 🇹🇷"
    expected = 'Turkey <tg-emoji emoji-id="99999">🌍</tg-emoji>'
    assert emoji_utils.apply_emoji(text) == expected


def test_apply_emoji_emoji_fallback(mock_emoji_map):
    """Test behavior for standard emoji fallback."""
    text = "Warning ⚠"
    expected = 'Warning <tg-emoji emoji-id="11111">\u26a0\ufe0f</tg-emoji>'
    assert emoji_utils.apply_emoji(text) == expected

    text2 = "Coin 🪙"
    expected2 = 'Coin <tg-emoji emoji-id="22222">💰</tg-emoji>'
    assert emoji_utils.apply_emoji(text2) == expected2


def test_apply_emoji_multiple_fallbacks(mock_emoji_map, monkeypatch):
    """Test behavior when an emoji has multiple fallbacks (takes the first mapped one)."""
    # \U0001F4E2 (📢) -> ['\U0001F50A', '\U0001F514', '\U0001F515'] (🔊 / 🔔 / 🔕)
    monkeypatch.setattr(emoji_utils, '_emoji_map', {
        '\U0001F514': '777', # 🔔 is mapped, but 🔊 is not
    })
    text = "Announcement 📢"
    expected = 'Announcement <tg-emoji emoji-id="777">🔔</tg-emoji>'
    assert emoji_utils.apply_emoji(text) == expected


def test_apply_emoji_no_match(mock_emoji_map):
    """Test behavior for emojis with no direct mapping or fallback."""
    text = "Unknown 👽"
    assert emoji_utils.apply_emoji(text) == text


def test_apply_emoji_mixed(mock_emoji_map):
    """Test behavior with a mix of emojis and text."""
    text = "Start 🇹🇷 processing ✅ but skip 🟠 and unknown 👽 with warning ⚠!"
    expected = 'Start <tg-emoji emoji-id="99999">🌍</tg-emoji> processing <tg-emoji emoji-id="12345">✅</tg-emoji> but skip 🟠 and unknown 👽 with warning <tg-emoji emoji-id="11111">\u26a0\ufe0f</tg-emoji>!'
    assert emoji_utils.apply_emoji(text) == expected
