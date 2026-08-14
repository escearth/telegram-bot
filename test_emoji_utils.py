import unittest
from unittest.mock import patch

# Import the module to be tested
import emoji_utils

class TestEmojiUtils(unittest.TestCase):

    def setUp(self):
        # We'll set up some dummy test emojis mapping for our tests
        self.mock_emoji_map = {
            '🍎': '12345',
            '🌍': '67890',
            '⚠️': '11111', # \u26a0\ufe0f
            '💰': '22222', # \U0001F4B0
            '👍': '33333',
        }

    def test_apply_emoji_empty_map(self):
        """Test that if _emoji_map is empty, the original text is returned."""
        with patch('emoji_utils._emoji_map', {}):
            text = "Here is an apple 🍎 and a globe 🌍!"
            self.assertEqual(emoji_utils.apply_emoji(text), text)

    def test_apply_emoji_skip(self):
        """Test that emojis in _EMOJI_SKIP are ignored."""
        with patch('emoji_utils._emoji_map', self.mock_emoji_map):
            text = "Orange circle 🟠 and blue circle 🔵"
            self.assertEqual(emoji_utils.apply_emoji(text), text)

    def test_apply_emoji_flag_fallback(self):
        """Test that flag pairs fallback to their defined fallbacks (like 🌍)."""
        with patch('emoji_utils._emoji_map', self.mock_emoji_map):
            text = "Flag of Turkey 🇹🇷"
            # 🇹🇷 is \U0001F1F9\U0001F1F7, maps to 🌍 (id 67890)
            expected = 'Flag of Turkey <tg-emoji emoji-id="67890">🌍</tg-emoji>'
            self.assertEqual(emoji_utils.apply_emoji(text), expected)

    def test_apply_emoji_exact_match(self):
        """Test that exactly matching emojis are wrapped correctly."""
        with patch('emoji_utils._emoji_map', self.mock_emoji_map):
            text = "I like 🍎 and 👍"
            expected = 'I like <tg-emoji emoji-id="12345">🍎</tg-emoji> and <tg-emoji emoji-id="33333">👍</tg-emoji>'
            self.assertEqual(emoji_utils.apply_emoji(text), expected)

    def test_apply_emoji_fallback(self):
        """Test that emojis use visually similar fallbacks from _EMOJI_FALLBACK."""
        with patch('emoji_utils._emoji_map', self.mock_emoji_map):
            text = "Warning ⚠ and Coin 🪙"
            # ⚠ (\u26a0) falls back to ⚠️ (\u26a0\ufe0f) id 11111
            # 🪙 (\U0001FA99) falls back to 💰 (\U0001F4B0) id 22222
            expected = 'Warning <tg-emoji emoji-id="11111">⚠️</tg-emoji> and Coin <tg-emoji emoji-id="22222">💰</tg-emoji>'
            self.assertEqual(emoji_utils.apply_emoji(text), expected)

    def test_apply_emoji_not_found(self):
        """Test that emojis not in the map or fallbacks are left as-is."""
        with patch('emoji_utils._emoji_map', self.mock_emoji_map):
            text = "Random emoji 🐉"
            self.assertEqual(emoji_utils.apply_emoji(text), text)

    def test_apply_emoji_complex(self):
        """Test a string with multiple replacements, fallbacks, skips, and text."""
        with patch('emoji_utils._emoji_map', self.mock_emoji_map):
            text = "Apple 🍎, Turkey 🇹🇷, Skip 🟠, Warn ⚠, Unknown 🐉!"
            expected = 'Apple <tg-emoji emoji-id="12345">🍎</tg-emoji>, Turkey <tg-emoji emoji-id="67890">🌍</tg-emoji>, Skip 🟠, Warn <tg-emoji emoji-id="11111">⚠️</tg-emoji>, Unknown 🐉!'
            self.assertEqual(emoji_utils.apply_emoji(text), expected)

if __name__ == '__main__':
    unittest.main()
