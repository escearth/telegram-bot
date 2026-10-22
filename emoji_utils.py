"""
Telegram animated emoji wrapper.

On startup, tries to fetch the official animated emoji sticker set via Telethon
(using API_ID / API_HASH from environment) and builds a mapping from emoji
character -> custom_emoji_id (document_id).  If Telethon is unavailable or
credentials are missing, falls back silently — emojis stay plain text.

Usage:
    from emoji_utils import apply_emoji, ensure_emoji_map

    ensure_emoji_map(logger)          # call once at startup
    html = apply_emoji("📊 BTC price")  # wrap known emojis
"""

import os
import re
import logging
import threading

try:
    import telethon
    import telethon.sync
    from telethon import functions, types
    _TELETHON_AVAILABLE = True
except ImportError:
    _TELETHON_AVAILABLE = False

# ---------------------------------------------------------------------------
# Regex matching any emoji in the Unicode ranges used by this bot
# ---------------------------------------------------------------------------
_EMOJI_PACKS_ENV = os.getenv('EMOJI_PACKS', 'RestrictedEmoji')
"""Comma-separated short names of custom emoji packs to fetch, e.g. ``RestrictedEmoji,MyPack``"""

_EMOJI_BASE = (
    '\U0001F000-\U0001FAFF'   # Supplemental symbols/pictographs incl. coin 🪙, folders, dice, flags range
    '\U00002600-\U00002BFF'   # Misc symbols, Arrows, Dingbats (⚖ ⛽ ✅ ☀ ⚠ ⬜ …)
    '\U00002100-\U0000214F'   # Letterlike symbols (ℹ …)
    '\U000023F0-\U000023FF'   # Time symbols (⏰ ⏳ ⏹ …)
)

# Matches full emoji sequences as ONE token:
#   - regional-indicator flag pairs
#   - keycaps (1️⃣ #️⃣ *️⃣)
#   - single emoji + any run of FE0F / skin-tone modifiers
#   - ZWJ families (👨‍👩‍👧‍👦) folded into one unit instead of three broken
#     sub-emojis (the old regex matched each code point separately)
_EMOJI_RE = re.compile(
    '('
    '[\U0001F1E6-\U0001F1FF]{2}'          # flag pair (2 regional indicators)
    r'|[0-9#\*]\ufe0f?\u20e3'             # keycap: digit/symbol + FE0F? + enclosing keycap
    '|[' + _EMOJI_BASE + ']'
    '(?:'
        '\ufe0f'                          # variation selector
        '|[\U0001F3FB-\U0001F3FF]'        # skin-tone modifiers
        '|\u200d[' + _EMOJI_BASE + ']'    # ZWJ + next member of the family
    ')*'
    ')'
)

# ---------------------------------------------------------------------------
# Fallback: emojis not found in any fetched pack → visually similar alternative
# that is more likely to have a custom-emoji ID.  The alt character replaces
# the original in the output so the animated version shows.
# ---------------------------------------------------------------------------
_EMOJI_FALLBACK: dict[str, list[str]] = {
    # Text-presentation -> emoji-presentation (adds FE0F — more likely in packs)
    '\u26a0': ['\u26a0\ufe0f'],       # ⚠ -> ⚠️
    '\u2696': ['\u2696\ufe0f'],       # ⚖ -> ⚖️
    '\u23f3': ['\u23f3\ufe0f'],       # ⏳ -> ⏳️
    '\u2600': ['\u2600\ufe0f'],       # ☀ -> ☀️
    '\u270f': ['\u270f\ufe0f'],       # ✏ -> ✏️
    '\u23f9': ['\u23f9\ufe0f'],       # ⏹ -> ⏹️
    '\u26fd': ['\u26fd\ufe0f'],       # ⛽ -> ⛽️
    '\U0001F5D1': ['\U0001F5D1\ufe0f'],     # 🗑 -> 🗑️
    '\U0001F3CE': ['\U0001F3CE\ufe0f'],     # 🏎 -> 🏎️
    '\U0001F32A': ['\U0001F32A\ufe0f'],     # 🌪 -> 🌪️
    '\U0001F5C2': ['\U0001F5C2\ufe0f'],     # 🗂 -> 🗂️
    '\U0001F441': ['\U0001F441\ufe0f'],     # 👁 -> 👁️
    '\u2139':    ['\u2139\ufe0f'],     # ℹ -> ℹ️
    # Uncommon / newer emojis -> more common alternatives (tried in order)
    '\U0001FA99': ['\U0001F4B0'],      # 🪙 -> 💰
    '\U0001F4CF': ['\U0001F4D0'],      # 📏 -> 📐
    '\U0001F4E4': ['\U0001F4E8'],      # 📤 -> 📨
    '\U0001F4E5': ['\U0001F4E8'],      # 📥 -> 📨
    '\U0001F947': ['\U0001F3C6', '\U0001F3C5'],  # 🥇 -> 🏆 / 🏅
    '\U0001F4F2': ['\U0001F4F1'],      # 📲 -> 📱
    '\U0001F52C': ['\U0001F50D', '\U0001F50E'],  # 🔬 -> 🔍 / 🔎
    '\U0001F527': ['\U0001F6E0\ufe0f'],  # 🔧 -> 🛠️
    '\U0001F465': ['\U0001F464', '\U0001FAC5'],  # 👥 -> 👤
    '\U0001F4E2': ['\U0001F50A', '\U0001F514', '\U0001F515'],  # 📢 -> 🔊 / 🔔 / 🔕
    '\U0001F389': ['\U0001F973', '\U0001F382', '\U0001F388'],  # 🎉 -> 🥳 / 🎂/🎈
    '\U0001F4B1': ['\U0001F4B2', '\U0001F4B0', '\U0001F4B5'],  # 💱 -> 💲 / 💰 / 💵
    '\U0001F4CB': ['\U0001F4DD'],      # 📋 -> 📝
    '\U0001F550': ['\U000023F0', '\U000023F1', '\U000023F2'],  # 🕐 -> ⏰ / ⌚
    '\U0001F512': ['\U0001F510', '\U0001F511'],  # 🔒 -> 🔐 / 🔑
    '\U0001F4B9': ['\U0001F4C8', '\U0001F4C9'],  # 💹 -> 📈 / 📉
    '\U0001F7E2': ['\u2705', '\U0001F49A'],      # 🟢 -> ✅ / 💚
    '\U00002B1C': ['\U0001F533', '\U0001F7E6', '\u2B1B'],  # ⬜ -> 🔳 / 🟦 / ⬛
    '\U0001F4B5': ['\U0001F4B2', '\U0001F4B0'],  # 💵 -> 💲 / 💰
    '\U0001F4E6': ['\U0001F4EB', '\U0001F4EC'],  # 📦 -> 📫 / 📬
    '\U0001F4C5': ['\U0001F4C6', '\U0001F4C7'],  # 📅 -> 📆 / 📇
    '\U0001F310': ['\U0001F30D', '\U0001F30E', '\U0001F30F'],  # 🌐 -> 🌍 / 🌎 / 🌏
    '\U0001F4CD': ['\U0001F4CC', '\U0001F4CE'],  # 📍 -> 📌 / 📎
    '\U0001F514': ['\U0001F50A', '\U0001F4E2'],  # 🔔 -> 🔊 / 📢
    '\U0001F4B2': ['\U0001F4B0'],      # 💲 -> 💰
}

# Fallback for flag pairs (regional indicator pairs) → geographic globe
_FLAG_FALLBACK: dict[str, str] = {
    '\U0001F1F9\U0001F1F7': '\U0001F30D',   # 🇹🇷 TR -> 🌍
    '\U0001F1EC\U0001F1E7': '\U0001F30D',   # 🇬🇧 GB -> 🌍
    '\U0001F1EE\U0001F1F7': '\U0001F30D',   # 🇮🇷 IR -> 🌍
}

_EMOJI_SKIP: set[str] = {
    '\U0001F7E0',   # 🟠 — user wants normal text
    '\U0001F535',   # 🔵 — user wants normal text
    '\U0001FA99',   # 🪙 — user wants normal text (sticker padding reads as extra space)
}

_emoji_map: dict[str, str] = {}         # emoji_char -> document_id string
_emoji_map_lock = threading.Lock()
_emoji_map_attempted = False            # a fetch already ran, even if it found nothing

# Telethon writes a .session file for this module; pin it to the module
# directory so it lands in a stable place regardless of the process CWD.
_EMOJI_SESSION_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'anon_emoji_fetcher'
)


def _telethon_available() -> bool:
    return _TELETHON_AVAILABLE


def _fetch_emoji_map_via_telethon(logger: logging.Logger) -> dict[str, str]:
    """Fetch custom emoji IDs from user-specified emoji packs (EMOJI_PACKS env var).

    The env var ``EMOJI_PACKS`` is a comma-separated list of pack short names,
    e.g. ``RestrictedEmoji,MyPack``.  Each pack is fetched via Telethon; emoji
    characters are mapped to their ``DocumentAttributeCustomEmoji`` document IDs.
    """
    api_id = os.getenv('TG_API_ID')
    api_hash = os.getenv('TG_API_HASH')
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')

    if not api_id or not api_hash:
        logger.info("emoji_utils: TG_API_ID / TG_API_HASH not set, skipping Telethon fetch")
        return {}

    if not bot_token:
        logger.warning("emoji_utils: TELEGRAM_BOT_TOKEN not set, cannot use Telethon")
        return {}

    pack_names = [p.strip() for p in _EMOJI_PACKS_ENV.split(',') if p.strip()]
    if not pack_names:
        logger.info("emoji_utils: EMOJI_PACKS is empty, nothing to fetch")
        return {}

    mapping: dict[str, str] = {}
    try:
        with telethon.sync.TelegramClient(
            _EMOJI_SESSION_FILE, int(api_id), api_hash,
        ).start(bot_token=bot_token) as client:
            for short_name in pack_names:
                try:
                    logger.info(f"emoji_utils: fetching emoji pack @{short_name}…")
                    result = client(functions.messages.GetStickerSetRequest(
                        stickerset=types.InputStickerSetShortName(short_name=short_name),
                        hash=0,
                    ))
                except Exception as e:
                    logger.warning(f"emoji_utils: pack @{short_name} not available: {e}")
                    continue

                packs: list[types.StickerPack] = getattr(result, 'packs', None) or []
                docs = getattr(result, 'documents', None) or []
                doc_map = {d.id: d for d in docs}
                count = 0
                for pack in packs:
                    emoji_char = pack.emoticon
                    for doc_id in pack.documents:
                        doc = doc_map.get(doc_id)
                        if doc and doc.attributes:
                            for attr in doc.attributes:
                                if isinstance(attr, types.DocumentAttributeCustomEmoji):
                                    mapping[emoji_char] = str(doc_id)
                                    count += 1
                                    break
                logger.info(f"emoji_utils: @{short_name} — mapped {count} emoji characters")

            # ── Fallback: official animated emoji animations ──────
            for set_type, label in [
                (types.InputStickerSetAnimatedEmojiAnimations, "animated emoji animations"),
            ]:
                try:
                    logger.info(f"emoji_utils: fetching {label} as fallback…")
                    result = client(functions.messages.GetStickerSetRequest(
                        stickerset=set_type(), hash=0,
                    ))
                except Exception as e:
                    logger.info(f"emoji_utils: {label} not available: {e}")
                    continue

                packs = getattr(result, 'packs', None) or []
                docs = getattr(result, 'documents', None) or []
                doc_map = {d.id: d for d in docs}
                fallback_count = 0
                for pack in packs:
                    emoji_char = pack.emoticon
                    if emoji_char in mapping:
                        continue  # already mapped by a user pack
                    for doc_id in pack.documents:
                        doc = doc_map.get(doc_id)
                        if doc and doc.attributes:
                            for attr in doc.attributes:
                                if isinstance(attr, types.DocumentAttributeCustomEmoji):
                                    mapping[emoji_char] = str(doc_id)
                                    fallback_count += 1
                                    break
                logger.info(f"emoji_utils: {label} — mapped {fallback_count} additional emoji characters")

        if not mapping:
            logger.info("emoji_utils: no custom-emoji documents found in any pack")
    except Exception as e:
        logger.warning(f"emoji_utils: Telethon fetch failed: {e}")

    return mapping


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_emoji_map(logger: logging.Logger | None = None) -> dict[str, str]:
    """Populate the emoji → document_id mapping.

    Called once at startup.  Silent fallback if Telethon is missing or
    credentials are not configured.

    The *attempted* flag also caches empty results, so a transient Telethon
    failure doesn't re-trigger the full multi-pack network fetch on every
    subsequent emoji message.  The network fetch runs OUTSIDE the lock so
    concurrent callers are never stalled behind slow RPCs; only the final
    assignment is locked.
    """
    global _emoji_map, _emoji_map_attempted
    with _emoji_map_lock:
        if _emoji_map or _emoji_map_attempted:
            return _emoji_map

    fetched: dict[str, str] = {}
    if _telethon_available():
        fetched = _fetch_emoji_map_via_telethon(logger or logging.getLogger(__name__))
    else:
        if logger:
            logger.info("emoji_utils: Telethon not installed; emojis will stay plain text")

    with _emoji_map_lock:
        if not _emoji_map:      # a concurrent fetch may already have populated it
            _emoji_map = fetched
        _emoji_map_attempted = True
        return _emoji_map


def apply_emoji(text: str) -> str:
    """Wrap known emoji characters in ``<tg-emoji>`` tags.

    Emojis not present in the mapping try ``_EMOJI_FALLBACK`` for a visually
    similar alternative.  If neither has a mapping the character is left as-is.
    Composite tokens (ZWJ families, FE0F variants) are wrapped member by
    member — each member keeps any FE0F, so 👨‍👩‍👧‍👦 renders as up to
    four animated members instead of being split at raw code-point level.
    """
    if not _emoji_map:
        return text

    def _wrap_member(core: str) -> str:
        """Wrap ONE sequence member (emoji char + any FE0F) if it maps."""
        # Skip list matches either presentation form
        if core in _EMOJI_SKIP or core.rstrip('\ufe0f') in _EMOJI_SKIP:
            return core
        candidate = core.endswith('\ufe0f') and core[:-1] or core
        # Exact token first, then text-presentation base (old lookup style)
        for cand in (core, candidate):
            eid = _emoji_map.get(cand)
            if not eid:
                alts = _EMOJI_FALLBACK.get(cand)
                if alts:
                    for alt in alts:
                        eid = _emoji_map.get(alt)
                        if eid:
                            return f'<tg-emoji emoji-id="{eid}">{alt}</tg-emoji>'
                if eid:
                    return f'<tg-emoji emoji-id="{eid}">{cand}</tg-emoji>'
        return core    # no mapping — leave plain

    def _replacer(m: re.Match) -> str:
        token = m.group(1)
        if token in _EMOJI_SKIP or token.rstrip('\ufe0f') in _EMOJI_SKIP:
            return m.group(0)
        # Whole token mapped directly (most common case)
        eid = _emoji_map.get(token)
        if eid:
            return f'<tg-emoji emoji-id="{eid}">{token}</tg-emoji>'
        # Flag fallback (flag pairs aren't in _emoji_map)
        flag_alt = _FLAG_FALLBACK.get(token)
        if flag_alt:
            eid = _emoji_map.get(flag_alt)
            if eid:
                return f'<tg-emoji emoji-id="{eid}">{flag_alt}</tg-emoji>'
        # Composite tokens (ZWJ families / FE0F variants): wrap per member
        if '\u200d' in token or '\ufe0f' in token:
            wrapped = ''.join(_wrap_member(member) for member in token.split('\u200d'))
            if wrapped != token:
                return wrapped
        return m.group(0)        # unchanged (preserves any FE0F in original)

    return _EMOJI_RE.sub(_replacer, text)
