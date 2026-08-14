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


def test_ensure_emoji_map_already_populated(monkeypatch):
    """Test that ensure_emoji_map returns immediately if _emoji_map is already populated."""
    mock_map = {'✅': '12345'}
    monkeypatch.setattr(emoji_utils, '_emoji_map', mock_map)

    # Mock _telethon_available to verify it is NOT called
    telethon_called = False
    def mock_telethon_available():
        nonlocal telethon_called
        telethon_called = True
        return True
    monkeypatch.setattr(emoji_utils, '_telethon_available', mock_telethon_available)

    result = emoji_utils.ensure_emoji_map()
    assert result == mock_map
    assert not telethon_called


def test_ensure_emoji_map_telethon_available(monkeypatch):
    """Test that ensure_emoji_map fetches the map when telethon is available and map is empty."""
    monkeypatch.setattr(emoji_utils, '_emoji_map', {})

    monkeypatch.setattr(emoji_utils, '_telethon_available', lambda: True)

    mock_fetched_map = {'✅': '54321'}
    def mock_fetch_emoji_map_via_telethon(logger):
        return mock_fetched_map
    monkeypatch.setattr(emoji_utils, '_fetch_emoji_map_via_telethon', mock_fetch_emoji_map_via_telethon)

    result = emoji_utils.ensure_emoji_map()
    assert result == mock_fetched_map
    assert emoji_utils._emoji_map == mock_fetched_map


def test_ensure_emoji_map_telethon_unavailable(monkeypatch):
    """Test that ensure_emoji_map logs and returns empty map when telethon is unavailable."""
    monkeypatch.setattr(emoji_utils, '_emoji_map', {})

    monkeypatch.setattr(emoji_utils, '_telethon_available', lambda: False)

    import logging
    mock_logger = logging.getLogger("test_emoji")

    # We will just verify it returns the empty map as expected
    result = emoji_utils.ensure_emoji_map(logger=mock_logger)
    assert result == {}
    assert emoji_utils._emoji_map == {}
