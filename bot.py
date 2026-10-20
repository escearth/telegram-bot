import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
import hmac
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from http import HTTPStatus
from urllib.parse import parse_qsl, urlparse, unquote, quote as url_quote
from io import BytesIO

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Global highly-optimized session.
# urllib3's default Retry(total=N) does NOT retry DNS/connect failures or read
# timeouts (connect/read default to False), which produced the "hostname could
# not be resolved" and "read timed out" errors in the logs. Enable them
# explicitly, plus status retries for rate-limit/server errors. GETs and the
# QuickChart chart POST are both idempotent enough to retry safely.
session = requests.Session()
retry = Retry(
    total=4,
    connect=4,
    read=4,
    status=4,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset({'GET', 'POST', 'PUT', 'HEAD', 'OPTIONS', 'DELETE', 'TRACE'}),
    respect_retry_after_header=True,
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
session.mount('http://', adapter)
session.mount('https://', adapter)

import waitress
import re
import time
import logging
import json
import threading
import functools
import sys
if hasattr(sys.stdout, "reconfigure"):
       sys.stdout.reconfigure(encoding="utf-8", errors="replace")
       sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import sqlite3
import hashlib
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
from pycoingecko import CoinGeckoAPI

import gc

import concurrent.futures
import html
from decimal import Decimal
import random
import pytz

# Enhanced number processing utilities
from number_utils import (
    parse_number,
    format_crypto,
    format_fiat,
    format_for_locale,
    format_wallet_balance,
    normalize_digits
)

# Animated emoji support (optional - requires Telethon + TG_API_ID/HASH)
from emoji_utils import apply_emoji, ensure_emoji_map

import simpleeval
from simpleeval import simple_eval

# Set simpleeval safety limits to prevent DoS attacks via huge numbers
simpleeval.MAX_POWER = 10000
simpleeval.MAX_STRING_LENGTH = 10000
simpleeval.MAX_SHIFT = 1000

cg = CoinGeckoAPI()
load_dotenv()

# Idempotent logging setup. Without this guard, re-imports / multiple entry
# points (bot.py, passenger_wsgi.py) can stack a second FileHandler+StreamHandler
# on the root logger, which shows up as every line printed twice in bot.log.
_log_root = logging.getLogger()
_log_path = str(Path('bot.log').resolve())
_log_have_file = _log_have_stream = False
for _h in list(_log_root.handlers):
    if isinstance(_h, logging.FileHandler) and str(Path(_h.baseFilename).resolve()) == _log_path:
        _log_have_file = True
    elif isinstance(_h, logging.StreamHandler):
        _log_have_stream = True
if not _log_have_file:
    _log_root.addHandler(logging.FileHandler('bot.log'))
if not _log_have_stream:
    _log_root.addHandler(logging.StreamHandler())
_log_root.setLevel(logging.INFO)
for _h in _log_root.handlers:
    _h.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not TELEGRAM_BOT_TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN not found!")
    exit(1)

# Optional: set OWNER_USER_ID to receive operational alerts (fallback rate, etc.)
_owner_env = os.getenv('OWNER_USER_ID')
OWNER_USER_ID: int | None = int(_owner_env) if _owner_env and _owner_env.isdigit() else None

# Force-join channel (username or invite link, e.g. '@my_channel' or 'https://t.me/my_channel')
REQUIRED_CHANNEL: str | None = os.getenv('REQUIRED_CHANNEL') or None
_CHANNEL_CHAT_ID: int | None = None


def _resolve_channel_id():
    global _CHANNEL_CHAT_ID
    if _CHANNEL_CHAT_ID is not None:
        return _CHANNEL_CHAT_ID
    if not REQUIRED_CHANNEL:
        return None
    try:
        chat = bot.get_chat(REQUIRED_CHANNEL)
        _CHANNEL_CHAT_ID = chat.id
    except Exception as e:
        logger.warning(f"Could not resolve channel {REQUIRED_CHANNEL}: {e}")
        _CHANNEL_CHAT_ID = None
    return _CHANNEL_CHAT_ID


# Suggestion system: forward user suggestions to this chat ID
_sug_env = os.getenv('SUGGESTION_CHAT_ID')
SUGGESTION_CHAT_ID: int | None = int(_sug_env) if _sug_env and (_sug_env.isdigit() or (_sug_env.startswith('-') and _sug_env[1:].isdigit())) else None

# Donation links / per-blockchain wallets
DONATION_LINK: str | None = os.getenv('DONATION_LINK') or None
DONATION_WALLETS: dict[str, str] = {}
for _chain in ['TRC20', 'TON', 'BEP20', 'ERC20', 'SOLANA']:
    _val = os.getenv(f'DONATION_{_chain}')
    if _val:
        DONATION_WALLETS[_chain] = _val

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
# Bind module-level shortcut for inline answering
answer_inline_query = bot.answer_inline_query
bot.answer_inline_query = bot.answer_inline_query


# ── Telegram API request reliability ───────────────────────────────────
# pyTelegramBotAPI ships with RETRY_ON_ERROR disabled and never retries HTTP
# 429/5xx responses, so a burst of 429 ("retry after N"), 502 Bad Gateway or a
# read-timeout just raises ApiTelegramException and gets logged as an error.
# Patch the low-level _make_request once so every bot.* call (send_message,
# answer_callback_query, getUpdates, ...) retries transient failures while
# honouring Telegram's Retry-After header. Idempotent: re-importing the module
# (e.g. under Passenger) must not re-wrap the wrapper.
TG_RETRY_STATUS = frozenset((429, 500, 502, 503, 504))
TG_MAX_ATTEMPTS = 5
_TG_RETRY_INSTALLED = False
_TG_RETRY_LOCK = threading.Lock()


def _telegram_retry_after(err) -> int | None:
    """Read Telegram's suggested retry_after from an ApiTelegramException."""
    try:
        params = (err.result_json or {}).get('parameters') or {}
        ra = params.get('retry_after')
        if isinstance(ra, (int, float)) and ra >= 0:
            return int(ra)
    except Exception:
        pass
    return None


def _install_telegram_retry():
    """Wrap telebot.apihelper._make_request with transient-error retries."""
    global _TG_RETRY_INSTALLED
    with _TG_RETRY_LOCK:
        if _TG_RETRY_INSTALLED:
            return
        from telebot import apihelper
        original = apihelper._make_request
        if getattr(original, '_tg_retry_wrapped', False):
            _TG_RETRY_INSTALLED = True
            return

        def _make_request_with_retry(token, method_name, method='get', params=None, files=None):
            last_error = None
            for attempt in range(TG_MAX_ATTEMPTS):
                try:
                    return original(token, method_name, method=method,
                                    params=params, files=files)
                except apihelper.ApiTelegramException as err:
                    last_error = err
                    # Only retry statuses that can succeed on a retry; a 400
                    # (bad request) would just fail again and burn calls.
                    if getattr(err, 'error_code', None) not in TG_RETRY_STATUS:
                        raise
                    wait = _telegram_retry_after(err) or min(2 ** attempt, 30)
                    logger.warning(
                        "Telegram API %s error %s: retrying in %ss (attempt %d/%d)",
                        method_name, getattr(err, 'error_code', '?'),
                        wait, attempt + 1, TG_MAX_ATTEMPTS,
                    )
                    time.sleep(wait)
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout) as err:
                    last_error = err
                    if attempt >= TG_MAX_ATTEMPTS - 1:
                        raise
                    wait = min(2 ** attempt, 15)
                    logger.debug("Telegram %s %s: retrying in %ss",
                                 method_name, type(err).__name__, wait)
                    time.sleep(wait)
                except requests.exceptions.RequestException as err:
                    last_error = err
                    if attempt >= TG_MAX_ATTEMPTS - 1:
                        raise
                    time.sleep(min(2 ** attempt, 15))
            raise last_error  # pragma: no cover - loop always returns/raises above

        _make_request_with_retry._tg_retry_wrapped = True
        apihelper._make_request = _make_request_with_retry
        # Let telebot's own layer also retry transient network failures so we
        # get two levels of protection for flaky connections.
        apihelper.RETRY_ON_ERROR = True
        apihelper.MAX_RETRIES = 3
        apihelper.RETRY_TIMEOUT = 1
        _TG_RETRY_INSTALLED = True
        logger.debug("Installed Telegram API retry wrapper")


_install_telegram_retry()

# ── Force-join channel helper ──────────────────────────────────────────
_joined_cache = {}
_joined_cache_lock = threading.Lock()

def _is_joined_channel(user_id: int) -> bool | None:
    """Check if user has joined REQUIRED_CHANNEL.
    Returns True/False or None if REQUIRED_CHANNEL is not configured."""
    if not REQUIRED_CHANNEL:
        return None
    with _joined_cache_lock:
        if user_id in _joined_cache:
            val, ts = _joined_cache[user_id]
            # Verified members: cache for an hour. Verified non-members
            # (False): short TTL so they aren't re-checked on every command.
            if time.time() - ts < (3600 if val else 300):
                return val
    try:
        _chat_id = _resolve_channel_id()
        if _chat_id is None:
            _chat_id = REQUIRED_CHANNEL
        member = bot.get_chat_member(_chat_id, user_id)
        result = member.status in ('member', 'administrator', 'creator')
    except Exception as e:
        logger.warning(f"Failed to check channel membership for user {user_id}: {e}")
        # Fail-OPEN on API errors: returning False here blocks real members
        # with a "join channel" prompt whenever Telegram/the network hiccups.
        # None = "unknown", and the caller only enforces on an explicit False.
        return None
    with _joined_cache_lock:
        _joined_cache[user_id] = (result, time.time())
    return result


def _send_join_required(chat_id: int):
    """Send a message asking the user to join the required channel."""
    channel_link = REQUIRED_CHANNEL if REQUIRED_CHANNEL.startswith('http') else f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 Join Channel", url=channel_link))
    kb.add(types.InlineKeyboardButton("✅ I've Joined", callback_data="check_join"))
    bot.send_message(
        chat_id,
        f"📢 <b>Please join our channel first!</b>\n"
        + quote(
            "You need to join the channel below to use this bot:\n"
            f"{REQUIRED_CHANNEL}\n\n"
            "After joining, click \"I've Joined\" to continue."
        ),
        parse_mode='HTML',
        reply_markup=kb
    )


# ── Animated emoji setup (initialised lazily in start_bot to avoid
#    import-time network calls to Telegram via Telethon) ───────────────
_TG_EMOJI_RE = re.compile(r'<tg-emoji[^>]*>|</tg-emoji>')

def _strip_html_tags(text: str) -> str:
    """Remove known inline HTML tags (<b>, <i>, <code>, <pre>, <a...>, <tg-emoji>, blockquote)
    so a message can be re-sent as plain text when HTML parsing fails."""
    return re.sub(
        r'</?(?:b|i|u|s|code|pre|a\b[^>]*|tg-emoji[^>]*|blockquote)?>',
        '', text
    )

def _strip_emoji_tags(text: str) -> str:
    """Remove <tg-emoji ...> and </tg-emoji> tags, leaving the emoji char."""
    return _TG_EMOJI_RE.sub('', text)


def _retry_without_emoji(e: Exception, text: str) -> str:
    """If the API rejects the message, strip tg-emoji tags and try again."""
    from telebot.apihelper import ApiTelegramException
    if isinstance(e, ApiTelegramException) and 'DOCUMENT_INVALID' in str(e):
        return _strip_emoji_tags(text)
    raise


# Monkey-patch bot's message-sending methods to wrap known emojis in
# <tg-emoji> tags when parse_mode='HTML'.
_orig_reply_to = bot.reply_to
_orig_send_message = bot.send_message
_orig_edit_message_text = bot.edit_message_text
_orig_send_photo = bot.send_photo
_orig_edit_message_caption = bot.edit_message_caption

def _emoji_reply_to(message, text, **kwargs):
    if kwargs.get('parse_mode') == 'HTML' and isinstance(text, str):
        emoji_text = apply_emoji(text)
        try:
            return _orig_reply_to(message, emoji_text, **kwargs)
        except Exception as e:
            text = _retry_without_emoji(e, text)
    return _orig_reply_to(message, text, **kwargs)

def _emoji_send_message(chat_id, text, **kwargs):
    if kwargs.get('parse_mode') == 'HTML' and isinstance(text, str):
        emoji_text = apply_emoji(text)
        try:
            return _orig_send_message(chat_id, emoji_text, **kwargs)
        except Exception as e:
            text = _retry_without_emoji(e, text)
    return _orig_send_message(chat_id, text, **kwargs)

def _emoji_edit_message_text(text, **kwargs):
    if kwargs.get('parse_mode') == 'HTML' and isinstance(text, str):
        emoji_text = apply_emoji(text)
        try:
            return _orig_edit_message_text(emoji_text, **kwargs)
        except Exception as e:
            text = _retry_without_emoji(e, text)
    return _orig_edit_message_text(text, **kwargs)

def _emoji_send_photo(chat_id, photo, **kwargs):
    caption = kwargs.get('caption')
    if caption and kwargs.get('parse_mode') == 'HTML':
        emoji_caption = apply_emoji(caption)
        try:
            return _orig_send_photo(chat_id, photo, **{**kwargs, 'caption': emoji_caption})
        except Exception as e:
            logger.warning(f"Failed to use emoji caption, falling back: {e}")
    return _orig_send_photo(chat_id, photo, **kwargs)

def _emoji_edit_message_caption(**kwargs):
    caption = kwargs.get('caption')
    if caption and kwargs.get('parse_mode') == 'HTML':
        emoji_caption = apply_emoji(caption)
        try:
            return _orig_edit_message_caption(**{**kwargs, 'caption': emoji_caption})
        except Exception as e:
            logger.warning(f"Failed to use emoji caption, falling back: {e}")
    return _orig_edit_message_caption(**kwargs)

bot.reply_to = _emoji_reply_to
bot.send_message = _emoji_send_message
bot.edit_message_text = _emoji_edit_message_text
bot.send_photo = _emoji_send_photo
bot.edit_message_caption = _emoji_edit_message_caption
# ──────────────────────────────────────────────────────────────────────

# Iran timezone for timestamps
IRAN_TZ = pytz.timezone('Asia/Tehran')

# Button panel security - track who initiated each callback
# Format: {message_id: {user_id: int, expires_at: timestamp}}
panel_owners = {}
panel_owners_lock = threading.Lock()
PANEL_TIMEOUT = 300  # 5 minutes until auto-delete

# Group chat slowdown monitoring (thread-safe)
# Format: {chat_id: [timestamps of recent messages]}
group_message_history = defaultdict(list)
group_slowdown_last_warning = {}  # {chat_id: timestamp of last warning}
group_activity_lock = threading.Lock()  # ← NEW: Protect shared state
SLOWDOWN_THRESHOLD = 40  # messages per minute
SLOWDOWN_COOLDOWN = 300  # 5 minutes between warnings

# Group-specific rate limiting
# Format: {chat_id: {user_id: [timestamps]}}
group_user_request_times = defaultdict(lambda: defaultdict(list))
group_user_rate_lock = threading.Lock()
GROUP_USER_RATE_LIMIT = 20      # max requests per user per window
GROUP_USER_RATE_WINDOW = 60     # per N seconds

# Group-wide request deduplication cache
# Format: {chat_id: {query_key: (result, timestamp)}}
group_query_cache = defaultdict(dict)
group_query_cache_lock = threading.Lock()
GROUP_QUERY_CACHE_TTL = 30  # seconds - short TTL for deduplication

# ─────────────────────────────────────────────
# SQLite persistence (replaces JSON files)
# ─────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_FILE = str(BASE_DIR / "bot_data.db")
db_lock = threading.Lock()


# Track last rate limit notification per user
_rate_limit_notified = {}
_rate_limit_notified_lock = threading.Lock()
RATE_LIMIT_NOTIFY_COOLDOWN = 60  # Only notify once per minute



import contextlib

_db_initialized = False


@contextlib.contextmanager
def get_db_conn():
    global _db_initialized
    if not _db_initialized:
        with db_lock:
            if not _db_initialized:
                init_db()
    # Use connection with WAL mode and busy timeout
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    try:
        # Enable WAL mode for better concurrency
        conn.execute("PRAGMA journal_mode=WAL")
        # Set busy timeout to 30 seconds
        conn.execute("PRAGMA busy_timeout=30000")
        # Enable foreign keys
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def init_db():
    global _db_initialized
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    try:
        # Enable WAL mode for better concurrency
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS holdings (
                user_id INTEGER PRIMARY KEY,
                data TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER,
                address TEXT,
                PRIMARY KEY (user_id, address)
            )
        """)
        # Price alerts: each row = one alert for one user on one coin
        c.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                crypto_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                target_price REAL NOT NULL,
                direction TEXT NOT NULL,   -- 'above' or 'below'
                created_at REAL NOT NULL
            )
        """)
        # Buy prices for P&L tracking (per user, per symbol)
        c.execute("""
            CREATE TABLE IF NOT EXISTS buy_prices (
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                buy_price REAL NOT NULL,
                PRIMARY KEY (user_id, symbol)
            )
        """)
        # Daily digest opt-in
        c.execute("""
            CREATE TABLE IF NOT EXISTS digest_prefs (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                hour INTEGER NOT NULL DEFAULT 9
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_languages (
                user_id INTEGER PRIMARY KEY,
                lang    TEXT NOT NULL DEFAULT 'en'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS exchange_prefs (
                user_id INTEGER PRIMARY KEY,
                exchange TEXT NOT NULL DEFAULT 'coingecko'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS currency_prefs (
                user_id INTEGER PRIMARY KEY,
                currency TEXT NOT NULL DEFAULT 'toman'
            )
        """)
        conn.commit()
    finally:
        conn.close()
    _db_initialized = True
    logger.info("Database initialised")


# ─────────────────────────────────────────────
# Language helpers
# ─────────────────────────────────────────────
_lang_cache: dict[int, str] = {}
_lang_cache_lock = threading.Lock()


def db_get_lang(user_id: int) -> str:
    with _lang_cache_lock:
        if user_id in _lang_cache:
            return _lang_cache[user_id]
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT lang FROM user_languages WHERE user_id=?", (user_id,))
        row = c.fetchone()
    lang = row[0] if row else 'en'
    with _lang_cache_lock:
        _lang_cache[user_id] = lang
    return lang


def _get_lang_cached(user_id: int) -> str:
    """Read language from cache only - no DB lock. For background threads."""
    with _lang_cache_lock:
        return _lang_cache.get(user_id, 'en')


def db_set_lang(user_id: int, lang: str) -> None:
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO user_languages (user_id, lang) VALUES (?,?)",
            (user_id, lang)
        )
        conn.commit()
    with _lang_cache_lock:
        _lang_cache[user_id] = lang


def T(user_id: int, key: str, **kwargs) -> str:
    """Return the translated string for user's language, with optional .format(**kwargs)."""
    lang = db_get_lang(user_id)
    text = STRINGS.get(lang, STRINGS['en']).get(key)
    if text is None:
        text = STRINGS['en'].get(key, f'[{key}]')
    return text.format(**kwargs) if kwargs else text


def _T_cached(user_id: int, key: str, **kwargs) -> str:
    """Like T() but uses cached language only - no DB lock. For background threads."""
    lang = _get_lang_cached(user_id)
    text = STRINGS.get(lang, STRINGS['en']).get(key)
    if text is None:
        text = STRINGS['en'].get(key, f'[{key}]')
    return text.format(**kwargs) if kwargs else text


def db_get_holdings(user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT data FROM holdings WHERE user_id=?", (user_id,))
        row = c.fetchone()
    return json.loads(row[0]) if row else None


def db_set_holdings(user_id, holdings_dict):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO holdings (user_id, data) VALUES (?,?)",
            (user_id, json.dumps(holdings_dict))
        )
        conn.commit()


def db_get_wallets(user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT address FROM wallets WHERE user_id=?", (user_id,))
        rows = c.fetchall()
    return [r[0] for r in rows]


def db_add_wallet(user_id, address):
    """Returns False if already exists, True on success."""
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO wallets (user_id, address) VALUES (?,?)", (user_id, address))
            conn.commit()
            success = True
        except sqlite3.IntegrityError:
            success = False
    return success


def db_clear_wallets(user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM wallets WHERE user_id=?", (user_id,))
        affected = c.rowcount
        conn.commit()
    return affected > 0


def db_remove_wallet(user_id, address):
    """Remove a single wallet. Returns True if it existed."""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM wallets WHERE user_id=? AND address=?", (user_id, address))
        affected = c.rowcount
        conn.commit()
    return affected > 0


def db_remove_holding(user_id, symbol):
    """Remove a single coin from holdings. Returns True on success."""
    data = db_get_holdings(user_id)
    if not data or symbol.upper() not in data:
        return False
    del data[symbol.upper()]
    if data:
        db_set_holdings(user_id, data)
    else:
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
            conn.commit()
    return True


# ── Alerts ────────────────────────────────────
def db_add_alert(user_id, crypto_id, symbol, target_price, direction):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO alerts (user_id, crypto_id, symbol, target_price, direction, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, crypto_id, symbol.upper(), target_price, direction, time.time())
        )
        alert_id = c.lastrowid
        conn.commit()
    return alert_id


def db_get_alerts(user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, crypto_id, symbol, target_price, direction FROM alerts WHERE user_id=? ORDER BY id",
            (user_id,)
        )
        rows = c.fetchall()
    return [{'id': r[0], 'crypto_id': r[1], 'symbol': r[2],
             'target_price': r[3], 'direction': r[4]} for r in rows]


def db_get_all_alerts():
    """Used by the alert checker thread."""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, user_id, crypto_id, symbol, target_price, direction FROM alerts")
        rows = c.fetchall()
    return [{'id': r[0], 'user_id': r[1], 'crypto_id': r[2],
             'symbol': r[3], 'target_price': r[4], 'direction': r[5]} for r in rows]


def db_delete_alert(alert_id, user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM alerts WHERE id=? AND user_id=?", (alert_id, user_id))
        affected = c.rowcount
        conn.commit()
    return affected > 0


def db_delete_alert_by_id(alert_id):
    """Used internally by the alert firing thread."""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
        conn.commit()


# ── Buy prices (P&L) ──────────────────────────
def db_set_buy_price(user_id, symbol, buy_price):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO buy_prices (user_id, symbol, buy_price) VALUES (?,?,?)",
            (user_id, symbol.upper(), buy_price)
        )
        conn.commit()


def db_get_buy_prices(user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT symbol, buy_price FROM buy_prices WHERE user_id=?", (user_id,))
        rows = c.fetchall()
    return {r[0]: r[1] for r in rows}


def db_delete_buy_price(user_id, symbol):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM buy_prices WHERE user_id=? AND symbol=?", (user_id, symbol.upper()))
        conn.commit()


# ── Daily digest ──────────────────────────────
def db_set_digest(user_id, enabled: bool, hour: int = 9):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO digest_prefs (user_id, enabled, hour) VALUES (?,?,?)",
            (user_id, int(enabled), hour)
        )
        conn.commit()


def db_get_digest(user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT enabled, hour FROM digest_prefs WHERE user_id=?", (user_id,))
        row = c.fetchone()
    return {'enabled': bool(row[0]), 'hour': row[1]} if row else None


def db_get_all_digest_users():
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, hour FROM digest_prefs WHERE enabled=1")
        rows = c.fetchall()
    return [{'user_id': r[0], 'hour': r[1]} for r in rows]


# ─────────────────────────────────────────────
# Helper Functions - Timestamps, Security, Monitoring
# ─────────────────────────────────────────────

def get_iran_time_str():
    """Get current date and time in Iran timezone as formatted string."""
    now = datetime.now(IRAN_TZ)
    return now.strftime('%Y-%m-%d %H:%M')  # Format: 2026-04-17 14:30

def add_timestamp(text: str) -> str:
    """Return message text unchanged (timestamp suffix removed)."""
    return text


def quote(text: str) -> str:
    """Wrap text in a Telegram HTML blockquote.

    The open tag starts the first content line and the close tag ends the
    last content line (no newline right after `<blockquote>` or right before
    `</blockquote>`), otherwise Telegram renders empty quoted lines at the
    top/bottom of the quote.
    """
    return "<blockquote>" + text.strip("\n") + "</blockquote>"


def _chg_str(chg, decimals: int = 2) -> str:
    """'📈 +2.35%' for a 24h change; '' when the change is unknown."""
    if chg is None:
        return ""
    arrow = '📈' if chg >= 0 else '📉'
    return f"{arrow} {chg:+.{decimals}f}%"


def _fg_bar(value: int) -> str:
    """10-cell Fear & Greed meter: 🟢🟢🟢⬜⬜…  (value 0-100)."""
    filled = max(0, min(10, round(value / 10)))
    return '🟢' * filled + '⬜' * (10 - filled)


def _fg_block(uid, fg_data) -> str:
    """Localized Fear & Greed meter block ('''🟢🟢…\n42/100 - Fear''').
    Used by /market and inline 'market' so both look identical."""
    if not fg_data:
        return T(uid, 'fg_unavailable')
    try:
        fg_val = int(fg_data.get('value', 50))
    except (TypeError, ValueError):
        return T(uid, 'fg_unavailable')
    fg_label_raw = fg_data.get('value_classification', '?')
    fg_label = {
        'Extreme Fear': T(uid, 'fg_extreme_fear'),
        'Fear':         T(uid, 'fg_fear'),
        'Neutral':      T(uid, 'fg_neutral'),
        'Greed':        T(uid, 'fg_greed'),
        'Extreme Greed':T(uid, 'fg_extreme_greed'),
    }.get(fg_label_raw, fg_label_raw)
    return f"{_fg_bar(fg_val)}\n<b>{fg_val}</b>/100 - {fg_label}"


def _sym(cid: str) -> str:
    """Extract ticker: '🪙 Bitcoin (BTC)' → 'BTC'"""
    entry = CRYPTO_LIST.get(cid)
    if entry and '(' in entry:
        return entry.split('(')[1].replace(')', '').strip()
    return cid.upper()


def register_panel_owner(chat_id: int, message_id: int, user_id: int):
    """Register who owns a button panel (message ids are only unique per chat)."""
    with panel_owners_lock:
        panel_owners[(chat_id, message_id)] = {
            'user_id': user_id,
            'expires_at': time.time() + PANEL_TIMEOUT
        }

def check_panel_owner(chat_id: int, message_id: int, user_id: int) -> bool:
    """Check if user owns the panel. Returns True if allowed."""
    key = (chat_id, message_id)
    with panel_owners_lock:
        if key not in panel_owners:
            return True  # No restriction if not registered

        panel = panel_owners[key]

        # Check if expired
        if time.time() > panel['expires_at']:
            del panel_owners[key]
            return True  # Expired, allow anyone

        # Check ownership
        return panel['user_id'] == user_id

def cleanup_expired_panels():
    """Remove expired panel ownership records."""
    current_time = time.time()
    with panel_owners_lock:
        expired = [mid for mid, panel in panel_owners.items() 
                   if current_time > panel['expires_at']]
        for mid in expired:
            del panel_owners[mid]

def monitor_group_activity(chat_id: int, message_time: float = None):
    """
    Monitor group message rate and return warning if too busy.
    Thread-safe: uses group_activity_lock.
    Returns warning message if threshold exceeded, None otherwise.
    """
    if message_time is None:
        message_time = time.time()
    
    with group_activity_lock:
        # Add current message
        group_message_history[chat_id].append(message_time)
        
        # Clean old messages (older than 1 minute)
        cutoff = message_time - 60
        group_message_history[chat_id] = [
            t for t in group_message_history[chat_id] if t > cutoff
        ]
        
        # Check if too busy
        msg_count = len(group_message_history[chat_id])
        
        if msg_count > SLOWDOWN_THRESHOLD:
            # Check cooldown
            last_warning = group_slowdown_last_warning.get(chat_id, 0)
            if message_time - last_warning > SLOWDOWN_COOLDOWN:
                group_slowdown_last_warning[chat_id] = message_time
                
                # Funny messages
                funny_messages = [
                    "😅 وای وای! گروه داره میسوزه! یه کم آروم‌تر لطفاً 🔥\n"
                    "Whoa! The chat is on fire! Slow down a bit please! 🔥",

                    "🚀 سرعتتون از صوت رد شد! بریک بزنید! 😄\n"
                    "You broke the sound barrier! Hit the brakes! 😄",

                    "🏎️ گروه تبدیل به اتوبان شده! محدودیت سرعت داریم اینجا 😂\n"
                    "The group became a highway! We have speed limits here! 😂",

                    "🌪️ گردباد پیام! یه نفس عمیق بکشید 😌\n"
                    "Message tornado! Take a deep breath! 😌"
                ]
                retry_hint = ("\n\n⏱ <i>Come back and try again in a minute.</i>\n"
                              "⏱ <i>حدود یک دقیقه دیگر دوباره امتحان کنید.</i>")
                return random.choice(funny_messages) + retry_hint
    
    return None

# ─────────────────────────────────────────────
# Thread-safe in-memory cache
# ─────────────────────────────────────────────
_cache = {}
_cache_lock = threading.Lock()
CACHE_TIMEOUT = 300  # 5 minute default cache for price data


def cache_get(key):
    """Get cached value, returns None if missing or expired. Cleans up stale entries."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        value, timestamp, ttl = entry
        if time.time() - timestamp >= ttl:
            del _cache[key]
            return None
        return value


def cache_set(key, value, ttl=None):
    """Set a cache entry with current timestamp and optional custom TTL (seconds)."""
    with _cache_lock:
        _cache[key] = (value, time.time(), ttl or CACHE_TIMEOUT)


def cache_cleanup():
    """Remove all expired entries from cache."""
    current_time = time.time()
    with _cache_lock:
        expired_keys = [
            k for k, (_, ts, ttl) in _cache.items() 
            if current_time - ts >= ttl
        ]
        for k in expired_keys:
            del _cache[k]
        if expired_keys:
            logger.debug(f"Cache cleanup: removed {len(expired_keys)} expired entries")


def cache_cleanup_loop():
    """Background thread that periodically purges stale cache entries."""
    while True:
        time.sleep(300)
        try:
            cache_cleanup()
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")


def _cleanup_user_state():
    """Purge expired user state entries (30 min TTL)."""
    now = time.time()
    with _user_state_lock:
        expired = [uid for uid, expiry in _user_state_ttl.items() if now > expiry]
        for uid in expired:
            user_state.pop(uid, None)
            _user_state_ttl.pop(uid, None)
        if expired:
            logger.debug(f"Cleaned {len(expired)} stale user state entries")


def _cleanup_user_state_loop():
    """Periodically purge expired user state entries (30 min TTL)."""
    while True:
        time.sleep(120)
        try:
            _cleanup_user_state()
        except Exception as e:
            logger.error(f"User state cleanup error: {e}")


# ─────────────────────────────────────────────
# Thread-safe rate limiter
# ─────────────────────────────────────────────
_api_lock = threading.Lock()
_last_api_call = 0.0
API_COOLDOWN = 3.0  # CoinGecko free tier: ~20 calls/min max


def rate_limited_api_call(func=None, *, cache_key=None):
    """Throttle outbound API calls to one per API_COOLDOWN seconds.

    Pass cache_key (a string, or a callable taking the wrapped function's
    arguments) for functions that return a cached value when one exists: a
    cache hit makes no network call, so it must not be throttled. Previously
    every cached get_usd_to_irr() paid a 3s sleep, and one inline query makes
    several such calls, so inline answers blew Telegram's answer deadline.
    """
    if func is None:
        return functools.partial(rate_limited_api_call, cache_key=cache_key)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _last_api_call
        if cache_key is not None:
            try:
                key = cache_key(*args, **kwargs) if callable(cache_key) else cache_key
                if cache_get(key):
                    return func(*args, **kwargs)
            except Exception:
                pass
        with _api_lock:
            now = time.time()
            wait = API_COOLDOWN - (now - _last_api_call)
            _last_api_call = time.time() + max(wait, 0)   # stamp before releasing lock
        if wait > 0:
            time.sleep(wait)                               # sleep outside lock
        return func(*args, **kwargs)                       # HTTP call outside the lock
    return wrapper


@rate_limited_api_call
def _fetch_prices_coingecko(ids) -> dict:
    if cache_get('cg_rate_limited'):
        return {}
    for attempt in range(2):
        try:
            resp = session.get(
                f"https://api.coingecko.com/api/v3/simple/price"
                f"?ids={ids}&vs_currencies=usd&include_24hr_change=true",
                timeout=8
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                cache_set('cg_rate_limited', True, ttl=60)
                time.sleep(2)
        except Exception as e:
            logger.error(f"CoinGecko batch error: {e}")
            time.sleep(1)
    return {}


@rate_limited_api_call(cache_key='cg_global')
def _fetch_coingecko_global() -> dict:
    """Fetch global market data from CoinGecko (rate-limited)."""
    cached = cache_get('cg_global')
    if cached:
        return cached
    try:
        resp = session.get("https://api.coingecko.com/api/v3/global", timeout=8)
        if resp.status_code == 200:
            data = resp.json().get('data', {})
            if data:
                cache_set('cg_global', data, ttl=120)
            return data
    except Exception as e:
        logger.error(f"CoinGecko global error: {e}")
    return {}


@rate_limited_api_call(cache_key='fng')
def _fetch_fear_greed() -> dict | None:
    """Fetch Fear & Greed index (rate-limited)."""
    cached = cache_get('fng')
    if cached:
        return cached
    try:
        resp = session.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        if resp.status_code == 200:
            data = resp.json().get('data', [{}])[0]
            if data:
                cache_set('fng', data, ttl=300)
            return data
    except Exception as e:
        logger.debug(f"Failed to fetch Fear & Greed: {e}")
    return None


_COINCAP_ID_MAP = {
    'binancecoin': 'binance-coin', 'the-open-network': 'toncoin',
}


def _fetch_prices_coincap(ids) -> dict:
    try:
        id_list = [i for i in ids.split(',') if i != 'telegram-stars']
        if not id_list:
            return {}
        cap_ids = ','.join(_COINCAP_ID_MAP.get(i, i) for i in id_list)
        resp = session.get(f"https://api.coincap.io/v2/assets?ids={cap_ids}", timeout=10)
        if resp.status_code != 200:
            return {}
        data = resp.json().get('data', [])
        lookup = {_COINCAP_ID_MAP.get(a['id'], a['id']): a for a in data}
        result = {}
        for cid in id_list:
            entry = lookup.get(cid)
            if entry:
                usd = float(entry.get('priceUsd', 0))
                change = float(entry.get('changePercent24Hr', 0))
                if usd:
                    result[cid] = {'usd': usd, 'usd_24h_change': change}
        return result
    except Exception as e:
        logger.error(f"CoinCap batch error: {e}")
        return {}


BINANCE_SKIP = {'tether', 'telegram-stars'}

# Internal id → exchange ticker symbol, shared by the price providers
# (Binance batch fetch, CryptoCompare fallback, exchange listing…).
EXCHANGE_SYMBOL_MAP = {
    'bitcoin': 'BTC', 'ethereum': 'ETH', 'tether': 'USDT',
    'binancecoin': 'BNB', 'cardano': 'ADA', 'ripple': 'XRP',
    'solana': 'SOL', 'polkadot': 'DOT', 'dogecoin': 'DOGE',
    'shiba-inu': 'SHIB', 'tron': 'TRX', 'the-open-network': 'TON',
    'avalanche-2': 'AVAX', 'chainlink': 'LINK', 'litecoin': 'LTC',
    'uniswap': 'UNI', 'stellar': 'XLM', 'monero': 'XMR',
    'near': 'NEAR', 'aptos': 'APT', 'sui': 'SUI',
    'cosmos': 'ATOM', 'algorand': 'ALGO', 'pepe': 'PEPE',
}


def _fetch_prices_binance(ids) -> dict:
    try:
        id_list = [c for c in ids.split(',') if c not in BINANCE_SKIP and c in EXCHANGE_SYMBOL_MAP]
        if not id_list:
            return {}
        symbols = [f"{EXCHANGE_SYMBOL_MAP[c]}USDT" for c in id_list]
        resp = session.get(
            "https://api.binance.com/api/v3/ticker/24hr"
            f"?symbols={json.dumps(symbols, separators=(',', ':'))}",
            timeout=10
        )
        if resp.status_code != 200:
            return {}
        arr = resp.json()
        sym_to_id = {v: k for k, v in EXCHANGE_SYMBOL_MAP.items()}
        result = {}
        for t in arr:
            sym = t.get('symbol', '').replace('USDT', '')
            cid = sym_to_id.get(sym)
            if cid:
                usd = float(t.get('lastPrice', 0))
                change = float(t.get('priceChangePercent', 0))
                if usd:
                    result[cid] = {'usd': usd, 'usd_24h_change': change}
        # Tether is always $1
        result['tether'] = {'usd': 1.0, 'usd_24h_change': 0}
        return result
    except Exception as e:
        logger.error(f"Binance batch error: {e}")
        return {}


def _fetch_prices_batch(ids: str) -> dict:
    cache_key = f'prices_batch_{ids}'
    cached = cache_get(cache_key)
    if cached:
        return cached
    for source_name, source_fn in [
        ('CoinGecko', _fetch_prices_coingecko),
        ('CoinCap', _fetch_prices_coincap),
        ('Binance', _fetch_prices_binance),
    ]:
        data = source_fn(ids)
        if data:
            logger.info(f"Batch prices from {source_name}")
            cache_set(cache_key, data, ttl=60)
            # Seed individual caches so get_crypto_price hits immediately
            for cid, info in data.items():
                usd = info.get('usd')
                if usd:
                    cache_set(cid, usd)
            return data
        logger.warning(f"{source_name} returned no data, trying next source")
    return {}


# ─────────────────────────────────────────────
# Per-user state (protected by lock for
# compound operations)
# ─────────────────────────────────────────────
user_state = {}
_user_state_lock = threading.Lock()
_user_state_ttl: dict[int, float] = {}


def get_user_state(user_id: int):
    with _user_state_lock:
        val = user_state.get(user_id)
        if val is not None:
            _user_state_ttl[user_id] = time.time() + 1800
        return val


def set_user_state(user_id: int, value):
    with _user_state_lock:
        user_state[user_id] = value
        _user_state_ttl[user_id] = time.time() + 1800


def del_user_state(user_id: int):
    with _user_state_lock:
        user_state.pop(user_id, None)
        _user_state_ttl.pop(user_id, None)

# ─────────────────────────────────────────────
# Per-user rate limiter
# Limits each user to USER_RATE_LIMIT requests
# per USER_RATE_WINDOW seconds.
# ─────────────────────────────────────────────
USER_RATE_LIMIT = 10       # max requests
USER_RATE_WINDOW = 60      # per N seconds
MAX_WALLETS_PER_USER = 10  # wallet cap
MAX_ALERTS_PER_USER = 10   # alert cap
REFRESH_COOLDOWN = 60      # seconds between refreshes (matches price update interval)
MAX_REFRESHES = 30         # max refreshes per window
REFRESH_WINDOW = 3600      # window in seconds (1 hour)

_user_request_times: dict[int, list[float]] = defaultdict(list)
_user_rate_lock = threading.Lock()


def is_user_rate_limited(user_id: int) -> bool:
    """
    Returns True if the user has exceeded USER_RATE_LIMIT
    requests in the last USER_RATE_WINDOW seconds.
    Does NOT record the timestamp - call record_user_request() after the handler runs.
    """
    now = time.time()
    with _user_rate_lock:
        times = _user_request_times.get(user_id)
        if not times:
            return False

        # Optimize: only clean if we might be at limit
        if len(times) >= USER_RATE_LIMIT:
            _user_request_times[user_id] = [t for t in times if now - t < USER_RATE_WINDOW]
            if len(_user_request_times[user_id]) >= USER_RATE_LIMIT:
                return True
        return False


def record_user_request(user_id: int):
    """Record a completed request timestamp for rate limiting."""
    now = time.time()
    with _user_rate_lock:
        _user_request_times[user_id].append(now)


def get_user_retry_seconds(user_id: int) -> int:
    """Seconds until the user drops below the command rate limit again.

    The limiter window slides, so the wait is the time remaining until the
    oldest still-counted request leaves the window (0 when not limited)."""
    now = time.time()
    with _user_rate_lock:
        times = [t for t in _user_request_times.get(user_id, [])
                 if now - t < USER_RATE_WINDOW]
    if len(times) < USER_RATE_LIMIT:
        return 0
    wait = USER_RATE_WINDOW - (now - min(times))
    return max(1, int(round(wait)))


# ─────────────────────────────────────────────
# Group-specific rate limiting
# ─────────────────────────────────────────────
def is_group_user_rate_limited(chat_id: int, user_id: int) -> bool:
    """Check if a user in a group has exceeded their rate limit."""
    now = time.time()
    with group_user_rate_lock:
        times = group_user_request_times[chat_id].get(user_id)
        if not times:
            return False
        if len(times) >= GROUP_USER_RATE_LIMIT:
            group_user_request_times[chat_id][user_id] = [t for t in times if now - t < GROUP_USER_RATE_WINDOW]
            if len(group_user_request_times[chat_id][user_id]) >= GROUP_USER_RATE_LIMIT:
                return True
        return False


def record_group_user_request(chat_id: int, user_id: int):
    """Record a completed request timestamp for group user rate limiting."""
    now = time.time()
    with group_user_rate_lock:
        group_user_request_times[chat_id][user_id].append(now)


def get_group_cached_query(chat_id: int, query_key: str):
    """Get cached raw data for a group query (for deduplication).
    Returns raw data dict, not rendered HTML. Returns None if missing/expired."""
    with group_query_cache_lock:
        entry = group_query_cache[chat_id].get(query_key)
        if entry is None:
            return None
        value, timestamp = entry
        if time.time() - timestamp >= GROUP_QUERY_CACHE_TTL:
            del group_query_cache[chat_id][query_key]
            return None
        return value


def set_group_cached_query(chat_id: int, query_key: str, value):
    """Cache raw data for a group query (for deduplication).
    Stores raw data dict, not rendered HTML."""
    with group_query_cache_lock:
        group_query_cache[chat_id][query_key] = (value, time.time())


def _cleanup_group_rate_limit_dicts():
    """Remove stale entries from group rate limiting dicts."""
    now = time.time()
    with group_user_rate_lock:
        stale_chats = []
        for chat_id, users in group_user_request_times.items():
            stale_users = [uid for uid, times in users.items()
                           if not times or now - max(times) > GROUP_USER_RATE_WINDOW * 2]
            for uid in stale_users:
                del group_user_request_times[chat_id][uid]
            if not group_user_request_times[chat_id]:
                stale_chats.append(chat_id)
        for cid in stale_chats:
            del group_user_request_times[cid]
    with group_query_cache_lock:
        stale_chats = []
        for chat_id, queries in group_query_cache.items():
            stale_queries = [k for k, (_, ts) in queries.items() if now - ts >= GROUP_QUERY_CACHE_TTL]
            for k in stale_queries:
                del group_query_cache[chat_id][k]
            if not group_query_cache[chat_id]:
                stale_chats.append(chat_id)
        for cid in stale_chats:
            del group_query_cache[cid]


def _format_cached_group_result(query_type: str, raw_data, user_id: int, text_lower: str, text_original: str) -> str:
    """Format cached raw data for a specific user's language."""
    if query_type == 'crypto_price':
        # raw_data is the price dict from API
        if isinstance(raw_data, dict) and 'price' in raw_data:
            price_usd = raw_data['price']
            change = raw_data.get('change')
            name = raw_data.get('name', '')
            sym = raw_data.get('sym', '')
            usd_to_irr = cache_get('usd_to_irr') or get_usd_to_irr()
            toman_line = _secondary_currency_line(user_id, price_usd, usd_to_irr)
            rows = f"💵 <b>{fmt_price(price_usd)}</b>" + (f"\n{toman_line}" if toman_line else "")
            return add_timestamp(
                f"📊 <b>{name}</b>\n" + quote(rows)
            )
    elif query_type in ('wallet_check', 'tx_check'):
        # raw_data can be string (old format) or dict with chain-specific data
        # (both query types render identically — one shared branch).
        # New format: stored messages are fully rendered (headers included),
        # so just join them; strings pass through unchanged.
        if isinstance(raw_data, dict):
            return add_timestamp("\n\n".join(str(v) for v in raw_data.values()))
        # Old format: string
        return add_timestamp(raw_data)
    # Fallback - return raw data as-is
    return add_timestamp(str(raw_data))


def _cache_group_query_result(query_type: str, query_key: str, chat_id: int, raw_data):
    """Cache raw data for a group query."""
    if query_type and raw_data:
        set_group_cached_query(chat_id, query_key, raw_data)


# ─────────────────────────────────────────────
# Refresh-button rate limiter
# Cooldown between refreshes + overall cap.
# Owner is exempt.
# ─────────────────────────────────────────────
_last_refresh_time: dict[int, float] = {}
_refresh_count: dict[int, list[float]] = defaultdict(list)
_refresh_limit_lock = threading.Lock()


def is_refresh_allowed(user_id: int) -> tuple[bool, str]:
    """Returns (allowed, reason_if_blocked). Owner always allowed."""
    if is_owner(user_id):
        return True, ""
    now = time.time()
    with _refresh_limit_lock:
        # Cooldown check
        last = _last_refresh_time.get(user_id, 0)
        if now - last < REFRESH_COOLDOWN:
            wait = int(REFRESH_COOLDOWN - (now - last))
            return False, T(user_id, 'refresh_cooldown', sec=wait)
        # Overall limit check
        times = _refresh_count[user_id]
        _refresh_count[user_id] = [t for t in times if now - t < REFRESH_WINDOW]
        if len(_refresh_count[user_id]) >= MAX_REFRESHES:
            return False, T(user_id, 'refresh_limit_reached')
        _refresh_count[user_id].append(now)
        _last_refresh_time[user_id] = now
        return True, ""


_last_memory_cleanup = 0.0
MEMORY_CLEANUP_INTERVAL = 300  # 5 minutes


def _cleanup_stale_dicts():
    """Remove stale entries from all unbounded dicts to prevent memory leaks."""
    now = time.time()
    # _rate_limit_notified
    with _rate_limit_notified_lock:
        stale = [uid for uid, ts in _rate_limit_notified.items()
                 if now - ts > RATE_LIMIT_NOTIFY_COOLDOWN]
        for uid in stale:
            del _rate_limit_notified[uid]
    # _joined_cache
    with _joined_cache_lock:
        stale = [uid for uid, (_, ts) in _joined_cache.items()
                 if now - ts > 3600]
        for uid in stale:
            del _joined_cache[uid]
    # _user_request_times - remove users with no recent activity
    with _user_rate_lock:
        stale = [uid for uid, times in _user_request_times.items()
                 if not times or now - max(times) > USER_RATE_WINDOW * 2]
        for uid in stale:
            del _user_request_times[uid]
    # _refresh_count and _last_refresh_time
    with _refresh_limit_lock:
        stale = [uid for uid, times in _refresh_count.items()
                 if not times or now - max(times) > REFRESH_WINDOW * 2]
        for uid in stale:
            del _refresh_count[uid]
            _last_refresh_time.pop(uid, None)
    # group_message_history and group_slowdown_last_warning
    with group_activity_lock:
        stale = [cid for cid, times in group_message_history.items()
                 if not times or now - max(times) > 120]
        for cid in stale:
            del group_message_history[cid]
            group_slowdown_last_warning.pop(cid, None)
    # Group rate limiting dicts
    _cleanup_group_rate_limit_dicts()
    # _webapp_rate_times - remove users with no recent activity
    with _webapp_rate_lock:
        stale = [uid for uid, times in _webapp_rate_times.items()
                 if not times or now - max(times) > WEBAPP_RATE_WINDOW * 2]
        for uid in stale:
            del _webapp_rate_times[uid]


_me_cache = None

KNOWN_COMMANDS = {
    'start', 'help', 'cancel', 'language', 'privacy', 'deleteaccount',
    'suggest', 'ticket', 'donate', 'donation', 'clearwallets', 'wallets',
    'mywallets', 'price', 'usd', 'try', 'eur', 'gbp', 'aed', 'cny', 'gold',
    'stars', 'star', 'set', 'holdings', 'convert', 'setexchange', 'setcurrency',
    'chart', 'trending', 'gainers', 'losers',
    'alert', 'alerts', 'compare', 'market', 'digest', 'test', 'admin',
}


def _get_me():
    global _me_cache
    if _me_cache is None:
        _me_cache = bot.get_me()
    return _me_cache


def _bot_username_tag() -> str:
    """'@username' for the running bot, resolved at runtime — the hardcoded
    literal used to break whenever the bot was renamed."""
    try:
        me = _get_me()
        un = getattr(me, 'username', None)
        return f"@{un}" if un else ""
    except Exception:
        return ""


def _classify_group_query(text_lower: str) -> str:
    """Classify a group plain-text query to determine rate limiting policy.
    Returns query type: 'crypto_price', 'fiat_conversion', 'math', 'wallet_check',
    'tx_check', 'gold_price', 'market_overview', 'command', 'wallet_add', 'alert_mgmt',
    'holdings_mgmt', 'unknown'.
    """
    if not text_lower:
        return 'unknown'
    # Callers pass raw message text, so normalize here: alias tables and the
    # address/keyword checks below are all lowercase.
    text_lower = str(text_lower).lower().strip()
    if not text_lower:
        return 'unknown'
    
    # Commands
    if text_lower.startswith('/'):
        return 'command'
    
    # Crypto price queries (btc, eth, 10 trx, Persian aliases like "بیت کوین")
    parts = text_lower.split()
    if len(parts) == 1 and parts[0] in CRYPTO_ALIASES:
        return 'crypto_price'
    if len(parts) == 2 and parts[1] in CRYPTO_ALIASES \
            and (parts[0].isdigit() or re.match(r'^[A-Za-z]{3,10}$', parts[0])):
        return 'crypto_price'
    # Multi-word Persian aliases (e.g. "بیت کوین") never match the ASCII-only
    # regex above, so check the whole phrase directly: the alias membership
    # itself is the real allowlist, the format was only a cheap pre-filter.
    if len(parts) in (1, 2) and text_lower in CRYPTO_ALIASES \
            and re.match(r'^[\w\s\u0600-\u06FF]{2,40}$', text_lower):
        return 'crypto_price'
    
    # Fiat conversion (100 usd to toman, etc.)
    if re.match(r'^([\d.,۰-۹٬٫]+)?\s*(\$|usd|dollar|دلار|lira|tl|try|₺|eur|euro|€|gbp|pound|£|aed|dirham|درهم|cny|yuan|یوآن)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال|usd|\$)?$', text_lower):
        return 'fiat_conversion'
    
    # Math expressions
    if re.match(r'^[\d+\-*/().%\s^]+$', text_lower) and any(c in text_lower for c in '+-*/%^'):
        return 'math'
    # "10% of 200" / "10% از 200" (handled by evaluate_math)
    if re.match(r'^\d+(?:\.\d+)?\s*%?\s*(?:of|از)\s*\d+(?:\.\d+)?$', text_lower):
        return 'math'
    
    # Wallet address check (TRON 34 chars, TON 48 chars EQ/UQ)
    if re.match(r'^[A-Za-z0-9]{34}$', text_lower) or (len(text_lower) == 48 and text_lower[:2] in ('eq', 'uq')):
        return 'wallet_check'
    
    # Transaction hash (64 hex chars) or transaction URLs
    if re.match(r'^[A-Fa-f0-9]{64}$', text_lower):
        return 'tx_check'
    if re.match(r'https?://(tronscan\.org/#/transaction|tonscan\.org/tx|tonviewer\.com/transaction)/', text_lower):
        return 'tx_check'
    
    # Gold price
    if text_lower in ('gold', 'gold price', 'طلا', 'قیمت طلا', 'xau'):
        return 'gold_price'
    
    # Market overview
    if text_lower in ('market', 'fear', 'greed', 'fear and greed', 'fear & greed', 'بازار', 'ترس و طمع'):
        return 'market_overview'
    
    # Standalone fiat rates
    if text_lower in ('usd', 'try', 'lira', 'tl', '₺', 'eur', 'euro', '€', 'gbp', 'pound', '£', 'aed', 'dirham', 'درهم', 'cny', 'yuan', 'یوآن'):
        return 'fiat_conversion'
    
    return 'unknown'


def _looks_like_bot_query(message):
    """Cheap pre-filter mirroring handle_text's matching rules. Used in groups
    so a reply to the bot only counts as bot-directed when it's a real query
    (coin symbol, math, fx rate, address, tx hash, state-machine input...)."""
    user_id = message.from_user.id if message.from_user else 0
    if not user_id:
        return False
    if get_user_state(user_id):
        return True
    text_original = (message.text or '').strip()
    if not text_original:
        return False
    text = _normalize_persian(text_original)
    text_lower = text.lower()

    if re.match(r'^[\d+\-*/().%\s^]+$', text) and len(text) > 1 and re.search(r'\d', text):
        return True
    if '% of' in text_lower or '%of' in text_lower or '% از' in text_lower \
            or '%از' in text_lower or '٪ از' in text_original or '٪از' in text_original:
        return True

    fx_patterns = [
        r'^([\d.,۰-۹٬٫]+)?\s*(\$|usd|dollar|دلار)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$',
        r'^([\d.,۰-۹٬٫]+)?\s*(lira|tl|try|₺)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$',
        r'^([\d.,۰-۹٬٫]+)?\s*(eur|euro|€)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$',
        r'^([\d.,۰-۹٬٫]+)?\s*(gbp|pound|£)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$',
        r'^([\d.,۰-۹٬٫]+)?\s*(aed|dirham|درهم)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$',
        r'^([\d.,۰-۹٬٫]+)?\s*(cny|yuan|yuen|یوآن)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$',
    ]
    for p in fx_patterns:
        if re.match(p, text_lower):
            return True

    if re.match(r'^[\d.,۰-۹٬٫]+\s*(gold|طل|طلا)\b', text_lower):
        return True
    if text_lower in ('gold', 'طل', 'طلا', 'xau', 'try', 'lira', 'tl', '₺'):
        return True
    if re.match(r'^[A-Za-z0-9]{34}$', text):
        return True
    if len(text) == 48 and text[:2] in ('EQ', 'UQ'):
        return True
    if re.match(r'^[A-Fa-f0-9]{64}$', text):
        return True
    if re.match(r'https?://(tronscan\.org/#/transaction|tonscan\.org/tx|tonviewer\.com/transaction)/', text):
        return True

    parts_raw = text.split()
    text_clean = parts_raw[0] if parts_raw else text
    if len(parts_raw) == 2 and parts_raw[1].startswith('@'):
        text_clean = parts_raw[0]
    if not re.search(r'\d', text) and len(parts_raw) == 1:
        crypto = detect_currency(text_clean)
        if crypto and crypto in CRYPTO_LIST:
            return True

    amount_crypto = re.match(r'^(\d+(?:\.\d+)?)\s*(\w+)$', text_lower)
    if amount_crypto and detect_currency(amount_crypto.group(2), check_u_alias=True):
        return True

    if len(text.split()) <= 4:
        for pattern in (r'^(\d+(?:\.\d+)?)\s*(\w+)\s*(?:to|به)\s*(\w+)$',
                        r'^(\d+(?:\.\d+)?)\s*(\w+)\s+(\w+)$'):
            m = re.search(pattern, text_lower)
            if m:
                src = detect_currency(m.group(2), check_u_alias=True)
                dst = detect_currency(m.group(3), check_u_alias=True)
                if src and dst:
                    return True

    return False


def _is_bot_directed_group_message(message):
    """In groups, only messages that clearly target the bot are processed:
    our registered commands, a @botusername mention, a reply to the bot
    that is an actual query, OR a recognized plain-text query (crypto symbol,
    wallet address, tx hash, math, fiat conversion, etc.). Everything else is fully ignored."""
    text = (message.text or '').strip()
    if not text:
        return False
    try:
        me = _get_me()
        username = getattr(me, 'username', None)
    except Exception as e:
        logger.debug(f"Failed to get bot username: {e}")
        return True  # fail-open: if we can't verify, don't block the bot

    if text.startswith('/'):
        parts = text[1:].split()
        if not parts:
            return False  # bare "/" — nothing to classify
        cmd = parts[0].split('@')[0].lower()
        return cmd in KNOWN_COMMANDS

    if username and ('@' + username.lower()) in text.lower():
        return True

    if message.reply_to_message and message.reply_to_message.from_user \
            and getattr(message.reply_to_message.from_user, 'id', None) == me.id:
        return _looks_like_bot_query(message)

    # Also accept recognized plain-text queries in groups (no mention/reply needed)
    # This allows users to just type "btc", "10 trx", wallet addresses, etc.
    if _looks_like_bot_query(message):
        return True

    return False


def _ignore_unknown_command(message):
    """Silently drop slash commands we don't own (private chats too), so no
    loading indicator / response is produced for them."""
    text = message.text or ''
    if not text.startswith('/'):
        return False
    parts = text[1:].split()
    if not parts:
        return False  # bare "/" — not a known command
    cmd = parts[0].split('@')[0].lower()
    if not cmd.isalpha():
        return False  # let slash-math etc. fall through to normal handling
    return cmd not in KNOWN_COMMANDS


def rate_limit_check(func):
    """Decorator - drops rate-limited updates and notifies user once per minute.
    Only counts bot commands (messages starting with /) toward the rate limit.
    Also monitors group command frequency for slowdown warnings.
    Records the request timestamp AFTER the handler completes so slow commands
    don't inflate the rate-limit window.
    For groups, applies per-user rate limiting and query deduplication.
    """
    @functools.wraps(func)
    def wrapper(message, *args, **kwargs):
        global _last_memory_cleanup
        # Channel/anonymous messages mirrored into supergroups have no
        # from_user — don't crash on them.
        user_id = message.from_user.id if message.from_user else 0
        if not user_id:
            return  # anonymous senders can't be rate-limited or joined-checked
        chat_id = message.chat.id

        # In groups, fully ignore messages that aren't meant for the bot
        # (no registration, no rate-limit, no fetch, no loading indicator).
        if message.chat.type in ('group', 'supergroup') \
                and not _is_bot_directed_group_message(message):
            return
        # Silently ignore slash commands we don't own, in any chat type.
        if _ignore_unknown_command(message):
            return

        # Periodic memory cleanup
        now = time.time()
        if now - _last_memory_cleanup > MEMORY_CLEANUP_INTERVAL:
            _cleanup_stale_dicts()
            _last_memory_cleanup = now

        is_command = (hasattr(message, 'text') and message.text
                      and message.text.startswith('/'))
        is_group = message.chat.type in ['group', 'supergroup']

        # Rate limiting: commands use global user rate limit, group plain-text uses group-specific limit
        if is_command:
            if is_user_rate_limited(user_id):
                logger.warning(f"User {user_id} rate-limited (command) - request dropped")
                current_time = time.time()
                with _rate_limit_notified_lock:
                    last_notified = _rate_limit_notified.get(user_id, 0)
                    if current_time - last_notified <= RATE_LIMIT_NOTIFY_COOLDOWN:
                        return
                    _rate_limit_notified[user_id] = current_time
                try:
                    lang = db_get_lang(user_id)
                    retry_s = get_user_retry_seconds(user_id)
                    if lang == 'fa':
                        msg = ("⏳ <b>آروم‌تر!</b>\n\n"
                               f"پیام‌ها را سریع می‌فرستی. لطفاً حدود <b>{retry_s} ثانیه</b> صبر کنید و دوباره امتحان کنید.")
                    else:
                        msg = ("⏳ <b>Slow down!</b>\n\n"
                               "You're sending messages too quickly.\n"
                               f"Please try again in <b>~{retry_s} seconds</b>.")
                    bot.reply_to(message, msg, parse_mode='HTML')
                except:
                    pass
                return
        elif is_group:
            # Group plain-text queries: classify query type first
            # Informational queries (crypto price, fiat conversion, math, wallet check, tx check)
            # should NOT be rate-limited per-user. Use provider-level caching and
            # request coalescing instead. Only stateful/command-like operations
            # (wallet add, alert set, holdings edit) need per-user limits.
            query_type = _classify_group_query(getattr(message, 'text', '') or '')
            if query_type in ('crypto_price', 'fiat_conversion', 'math', 'wallet_check', 'tx_check', 'gold_price', 'market_overview'):
                # These are read-only informational queries - no per-user rate limit
                # Provider-level caching + request deduplication handles load
                pass
            else:
                # Commands, wallet additions, alert management, etc. - apply rate limit
                if is_group_user_rate_limited(chat_id, user_id):
                    logger.warning(f"User {user_id} in group {chat_id} rate-limited (group query: {query_type}) - request dropped")
                    return  # Silently drop to avoid spam in groups

        # Force-join check for all bot commands (not regular chat messages)
        if is_command and REQUIRED_CHANNEL and _is_joined_channel(user_id) is False:
            _send_join_required(chat_id)
            return

        # Monitor group command rate for slowdown warnings (bot commands only)
        if is_command and is_group:
            warning = monitor_group_activity(chat_id, time.time())
            if warning:
                try:
                    bot.send_message(chat_id, warning)
                except:
                    pass

        if is_command:
            record_user_request(user_id)
        elif is_group:
            record_group_user_request(chat_id, user_id)

        try:
            return func(message, *args, **kwargs)
        finally:
            # Record AFTER the handler completes so slow commands don't
            # inflate the rate-limit window (matches the docstrings).
            if is_command:
                record_user_request(user_id)
            elif is_group:
                record_group_user_request(chat_id, user_id)
    return wrapper


def loading_indicator(func=None, *, keep=False):
    """Decorator - shows a 'Fetching…' placeholder while a handler works,
    then removes it once the real output has been sent. With keep=True the
    placeholder stays in the chat. Degrades gracefully: if the placeholder
    can't be sent/deleted, the handler still runs."""
    def deco(f):
        @functools.wraps(f)
        def wrapper(message, *args, **kwargs):
            loading = None
            try:
                lang = db_get_lang(message.from_user.id)
                text = "⏳ <i>در حال دریافت…</i>" if lang == 'fa' else "⏳ <i>Fetching…</i>"
                loading = bot.reply_to(message, text, parse_mode='HTML')
            except Exception as e:
                logger.debug(f"Failed to send loading indicator: {e}")
            try:
                return f(message, *args, **kwargs)
            finally:
                if loading is not None and not keep:
                    try:
                        bot.delete_message(loading.chat.id, loading.message_id)
                    except Exception as e:
                        logger.debug(f"Failed to delete loading message: {e}")
        return wrapper
    return deco(func) if func is not None else deco


# ─────────────────────────────────────────────
# i18n string table  (en + fa)
# Add new strings here - never hardcode in handlers
# ─────────────────────────────────────────────
STRINGS = {
    'en': {
        # ── Generic ───────────────────────────────────────────
        'cancelled':           "✅ Cancelled.",
        'nothing_to_cancel':   "ℹ️ Nothing to cancel.",
        'something_went_wrong': "❌ Something went wrong. Please try again.",
        'slow_down':           "⏳ Slow down a little! Try again in about {secs} seconds.",
        'unknown_coin':        "❌ Unknown coin: <code>{sym}</code>\n\nTry: BTC, ETH, TRX, SOL…",
        'price_unavailable':   "⚠️ Prices unavailable right now. Try again in a moment.",
        'market_unavailable':  "❌ Market data unavailable. Try again later.",
        'invalid_amount':      "❌ Invalid amount. Send a positive number like <code>0.5</code>.",
        'invalid_price':       "❌ Invalid price. Send a positive number.",
        'invalid_expression':  "❌ Invalid expression.",
        'division_by_zero':    "❌ Division by zero.",
        'math_result':         "✅ {expr} = {result}",
        'invalid_hour':        "❌ Please send a number between 0 and 23.",
        'price_fetch_fail':    "❌ Can\'t fetch price right now. Try again in a moment.",
        'refreshing':          "Refreshing…",
        'refresh_cooldown':    "⏳ Wait {sec}s before refreshing again.",
        'refresh_limit_reached': "⚠️ Refresh limit reached. Try again later.",
        'generating_chart':    "Generating chart…",
        'fetching':            "Fetching…",

        # ── Loading placeholders (⏳ + italic, used everywhere) ──
        'loading_prices':      "⏳ <i>Fetching prices…</i>",
        'loading_chart':       "⏳ <i>Generating chart…</i>",
        'loading_portfolio':   "⏳ <i>Building portfolio chart…</i>",
        'loading_market':      "⏳ <i>Pulling market data…</i>",
        'loading_compare':     "⏳ <i>Comparing coins…</i>",

        # ── Language selection ────────────────────────────────
        'lang_prompt':         "🌐 <b>Choose your language</b>\nزبان خود را انتخاب کنید:",
        'lang_set_en':         "✅ Language set to <b>English</b>.",
        'lang_set_fa':         "✅ زبان به <b>فارسی</b> تغییر یافت.",

        # ── /start ────────────────────────────────────────────
        'start_welcome':
            "🌍 <b>Earth Crypto, {name}! 🚀</b>\n"
            "<i>Prices · Charts · Portfolio · Alerts · Wallets</i>\n\n"
            "📈 <b>Prices &amp; Charts</b>\n"
            "<blockquote><code>/price</code> — all coins with 24h change\n"
            "<code>/chart</code> — price chart (coin + range via buttons)\n"
            "<code>/compare</code> — compare two coins (via buttons)\n"
            "<code>/gold</code> — gold prices (global + Iran)\n"
            "<code>/market</code> — Fear &amp; Greed overview\n"
            "<code>/trending</code> — top 10 trending coins\n"
            "<code>/gainers</code> — top gainers in 24h\n"
            "<code>/losers</code> — top losers in 24h</blockquote>\n\n"
            "💱 <b>Currency &amp; Conversion</b>\n"
            "<blockquote><code>/usd</code> — US Dollar to Toman\n"
            "<code>/convert</code> — coins · USD · Toman converter\n"
            "<code>/star</code> — Telegram Stars price\n"
            "<code>/setcurrency</code> — default display currency\n"
            "<code>/setexchange</code> — default price source</blockquote>\n\n"
            "💼 <b>Portfolio · Alerts · Wallets</b>\n"
            "<blockquote><code>/set</code> — update your holdings\n"
            "<code>/holdings</code> — portfolio P&amp;L snapshot\n"
            "<code>/digest</code> — daily summary settings\n"
            "<code>/alert</code> — set a new price alert\n"
            "<code>/alerts</code> — manage your alerts\n"
            "<code>/wallets</code> — manage TRON \x26amp; TON addresses\n"
            "<code>/mywallets</code> — live wallet balances\n"
            "<code>/clearwallets</code> — remove all saved wallets</blockquote>\n\n"
            "⚙️ <b>Settings &amp; More</b>\n"
            "<blockquote><code>/language</code> — switch language (English / فارسی)\n"
            "<code>/suggest</code> — send feedback or bug report\n"
            "<code>/donate</code> — support the project\n"
            "<code>/privacy</code> — privacy &amp; data policy\n"
            "<code>/deleteaccount</code> — delete all your data</blockquote>\n\n"
            "⚡ <b>Just type:</b> <code>btc</code>, <code>10 eth</code>, <code>100 usd</code>\n\n"
            "💖 /donate · 🔒 /privacy · 🌐 /language\n"
            "<i>Inline:</i> <code>{bot_username}</code> in any chat 🚀",

        # ── /language ─────────────────────────────────────────
        'btn_english':         "🇬🇧 English",
        'btn_persian':         "🇮🇷 فارسی",

        # ── /suggest, /donate ──────────────────────────────────
        'suggest_prompt':
            "💡 <b>Send your suggestion, bug report, or feedback</b>\n\n"
            "Your message will be forwarded to the bot owner. Send /cancel to cancel.",
        'suggest_sent':        "✅ Your suggestion has been sent. Thank you!",
        'suggest_fail':        "❌ Failed to send suggestion. Please try again later.",
        'suggest_no_config':   "The suggestion system is not configured.",
        'donate_info':
            "💖 <b>Support Earth Crypto</b>\n\n"
            "If you find this bot useful, consider supporting the project!",
        'donate_no_config':    "No donation information configured.",

        # ── /privacy ──────────────────────────────────────────
        'privacy_text':
            "🔒 <b>Privacy &amp; Data</b>\n\n"
            "Earth Crypto stores the following data linked to your Telegram user ID:\n\n"
            "• <b>Holdings</b> - coin amounts you set via /set or /holdings\n"
            "• <b>Buy prices</b> - average buy prices for P&amp;L tracking\n"
            "• <b>Wallets</b> - TRON & TON wallet addresses you add via /wallets\n"
            "• <b>Alerts</b> - price alert targets you set via /alert\n"
            "• <b>Digest preference</b> - your daily digest on/off setting and time\n"
            "• <b>Language</b> - your chosen interface language\n"
            "• <b>Display currency</b> - your chosen display currency (Toman by default)\n\n"
            "No names, messages, or personal information are stored beyond what you explicitly provide.\n\n"
            "Your data is stored on the server running this bot and is never sold or shared with third parties.\n\n"
            "To delete all your data permanently, use:\n"
            "<code>/deleteaccount</code>",

        # ── /deleteaccount ────────────────────────────────────
        'delete_confirm_prompt':
            "⚠️ <b>Delete all your data?</b>\n\n"
            "This will permanently remove:\n"
            "• All holdings &amp; buy prices\n"
            "• All wallet addresses\n"
            "• All price alerts\n"
            "• Your digest preferences\n"
            "• Your language setting\n\n"
            "This cannot be undone.",
        'btn_delete_yes':      "🗑 Yes, delete everything",
        'btn_cancel':          "❌ Cancel",
        'delete_done':
            "✅ <b>All your data has been deleted.</b>\n\n"
            "You can start fresh anytime with /start.",

        # ── Wallets ───────────────────────────────────────────
        'no_wallets':          "⚠️ No saved wallets.\n\nTap ➕ to add one.",
        'wallets_header':      "👛 <b>Your Saved Wallets</b>\n",
        'wallet_added':        "✅ <b>Wallet added!</b>\n\n<code>{address}</code>\n\nYou now have {count}/{max} wallets saved.",
        'wallet_limit':        "⚠️ Wallet limit reached ({max} max).\nUse /wallets to remove one first.",
        'wallet_invalid':      "❌ <b>Invalid address</b>\n\n<code>{address}</code>\n\nSupported formats:\n• <b>TRON</b> — starts with <b>T</b>, 34 characters\n• <b>TON</b> — starts with <b>EQ</b> or <b>UQ</b>, 48 characters\n\nPlease double-check and try again.",
        'wallet_already_saved':"⚠️ This wallet is already saved.",
        'wallet_not_found':    "Wallet not found - list may have changed.",
        'wallet_header_tron':  "👛 <b>TRON Wallet</b>\n",
        'wallet_header_ton':   "👛 <b>TON Wallet</b>\n",
        'wallet_removed_toast':"✅ Wallet removed.",
        'no_wallets_yet':      "⚠️ No saved wallets yet.\nUse /wallets to manage them.",
        'wallets_balances_hdr':"👛 <b>Wallets &amp; Balances</b>\n\n",
        'btn_view_wallets':    "👁 View Wallets",
        'btn_add_wallet':      "➕ Add Wallet",
        'btn_close':           "❌ Close",
        'btn_remove':          "🗑 Remove",
        'send_wallet_addr':    "🔗 Send your TRON or TON wallet address:\n(or /cancel to abort)",
        'no_balance':          "❌ No balance found for this address.",
        'trx_balance':         "👛 TRX Balance: {bal} TRX",
        'tron_timeout':        "⏳ Request timed out. Try again.",
        'tron_error':          "❌ Could not reach TRON network. Try again.",
        'invalid_tron_addr':   "❌ That doesn\'t look like a valid TRON address. Please check it and try again.",
        'invalid_address':     "❌ Invalid address. Please check it and try again.",

        # ── Holdings ──────────────────────────────────────────
        'no_holdings':         "⚠️ No holdings yet.\n\nTap ➕ to add one.",
        'portfolio_header':    "💼 <b>Your Portfolio</b>\n",
        'price_unavail_short': "<i>price unavailable</i>",
        'buy_at':              "buy@{price}",
        'chart_title':         "Portfolio Breakdown  (Total: ${total})",
        'chart_caption':       "📊 <b>Portfolio Breakdown</b>",
        'price_chart_title':   "{sym} Price Chart (Last {days} Days)",
        'price_chart_xlabel':  "Date",
        'price_chart_ylabel':  "Price (USD)",
        'portfolio_total':     "\n💰 <b>Total: {usd}</b> · {irr} Toman",
        'portfolio_total_cur': "\n💰 <b>Total: {usd}</b> · {val} {cur}",
        'holding_set':         "✅ <b>{sym}</b> set to <b>{amount}</b>\n\n",
        'holding_added':       "✅ Added <b>{amount}</b> {sym} (total: <b>{total}</b>)\n\n",
        'btn_add_coin':        "➕ Add Coin",
        'btn_add_another':     "➕ Add Another Coin",
        'btn_chart':           "📊 Chart",
        'btn_clear_all':       "🗑 Clear All",
        'btn_edit':            "✏️ Edit",
        'btn_buy_price':       "💲 Entry Price",
        'edit_amount_prompt':  "✏️ Send new amount for <b>{sym}</b>:\n(or /cancel to abort)",
        'buy_price_prompt':    "💲 Send your average <b>buy price (USD)</b> for <b>{sym}</b>:\ne.g. <code>85000</code>\n\n(or /cancel to abort)",
        'no_holdings_chart':   "⚠️ No holdings to chart yet.",
        'chart_fail':          "❌ Could not generate chart. Make sure your holdings have valid prices.",
        'clear_all_prompt':    "⚠️ Clear <b>all</b> holdings?",
        'btn_yes_clear':       "✅ Yes, clear all",
        'holdings_cleared':    "✅ All holdings cleared.",
        'set_holdings_prompt': "💼 <b>Set Holdings</b>\n\nPick a coin to set your amount for:",
        'add_coin_prompt':     "💼 <b>Add Coin</b>\n\nWhich coin do you want to add?",
        'coin_amount_prompt':  "💼 <b>{sym}</b>{price}\n\nHow much <b>{sym}</b> do you hold?\n<i>e.g. <code>0.5</code></i>\n\nType /cancel to abort.",
        'now_price':           "  (now {price})",
        'unknown_coin_short':  "Unknown coin.",
        'holding_removed_toast': "✅ {sym} removed.",
        'invalid_amount_conv': "❌ Invalid amount. Send a number like <code>10</code>.",

        # ── /price ────────────────────────────────────────────
        'prices_header':       "📊 <b>Live Crypto Prices</b>  (24h)\n",
        'btn_refresh':         "🔄 Refresh",
        'updated_at':          "Updated {time}",

        # ── /usd ──────────────────────────────────────────────
        'usd_rate':            "<blockquote>💵 <b>1 USD = {rate} Toman</b></blockquote>",
        'usd_conversion':      "<blockquote>💵 <b>{amount} USD = {result} Toman</b></blockquote>",
        'rate_unavailable':    "⚠️ Exchange rate unavailable right now.\n\nTry again in a moment.",

        # ── /try (Turkish Lira) ───────────────────────────────
        'try_rate':            "<blockquote>🇹🇷 <b>1 TRY = {rate} Toman</b></blockquote>",
        'try_conversion':      "<blockquote>🇹🇷 <b>{amount} TRY = {result} Toman</b></blockquote>",

        # ── /eur /gbp /aed /cny (fiat rates) ─────────────────
        'eur_rate':            "<blockquote>🇪🇺 <b>1 EUR = {rate} Toman</b></blockquote>",
        'eur_conversion':      "<blockquote>🇪🇺 <b>{amount} EUR = {result} Toman</b></blockquote>",
        'gbp_rate':            "<blockquote>🇬🇧 <b>1 GBP = {rate} Toman</b></blockquote>",
        'gbp_conversion':      "<blockquote>🇬🇧 <b>{amount} GBP = {result} Toman</b></blockquote>",
        'aed_rate':            "<blockquote>🇦🇪 <b>1 AED = {rate} Toman</b></blockquote>",
        'aed_conversion':      "<blockquote>🇦🇪 <b>{amount} AED = {result} Toman</b></blockquote>",
        'cny_rate':            "<blockquote>🇨🇳 <b>1 CNY = {rate} Toman</b></blockquote>",
        'cny_conversion':      "<blockquote>🇨🇳 <b>{amount} CNY = {result} Toman</b></blockquote>",

        # ── /gold ─────────────────────────────────────────────
        'gold_hdr':            "🥇 <b>Gold</b>\n",
        'gold_global':         "🌍 <i>Global · XAU/USD</i>\n💵 Ounce: <b>${xau}</b>\n💵 Gram: <b>${gram}</b>",
        'gold_iran':           "🇮🇷 <b>Iran · Toman</b>\n",
        'gold_conv_hdr':       "🥇 <b>{amount} g</b>\n",
        'gold_bahar':          "🪙 Bahar Azadi: {price}\n",
        'gold_emami':          "🪙 Emami: {price}\n",
        'gold_nim':            "🪙 Nim (½): {price}\n",
        'gold_rob':            "🪙 Rob (¼): {price}\n",
        'gold_gram18':         "⚖️ 18K Gold/g: {price}\n",
        'gold_gram24':         "⚖️ 24K Gold/g: {price}\n",
        'gold_mesghal':        "⚖️ Mesghal: {price}\n",
        'gold_fetch_fail':     "❌ Could not fetch gold prices. Try again.",

        # ── /market ───────────────────────────────────────────
        'market_header':       "🌍 <b>Crypto Market</b>\n",
        'market_mcap':         "💹 Market Cap: <b>{mcap}</b>  {arrow} {chg}% (24h)\n",
        'market_vol':          "📊 24h Volume: <b>{vol}</b>\n",
        'market_dom':          "🟠 BTC Dominance: <b>{btc}%</b>  🔵 ETH: <b>{eth}%</b>\n",
        'market_coins':        "🪙 Active Coins: <b>{coins}</b>\n",
        'market_fg':           "😨 <b>Fear &amp; Greed</b>\n{fg}",
        'fg_unavailable':      "Unavailable",
        'fg_extreme_fear':     "Extreme Fear",
        'fg_fear':             "Fear",
        'fg_neutral':          "Neutral",
        'fg_greed':            "Greed",
        'fg_extreme_greed':    "Extreme Greed",

        # ── /compare ──────────────────────────────────────────
        'compare_header':      "📊 <b>Comparison</b>\n",
        'compare_pick1':       "📊 <b>Compare Coins</b>\n\nPick the <b>first</b> coin:",
        'compare_pick2':       "📊 <b>Compare Coins</b>\n\n✅ First: <b>{sym}</b>\n\nNow pick the <b>second</b> coin:",
        'compare_vol':         "📦 Vol 24h: {vol}\n",
        'compare_mcap':        "💹 MCap: {mcap}",
        'compare_winner':      "\n🏆 <b>{name}</b> performed better in the last 24h",
        'compare_tied':        "\n🤝 <b>Tied</b> - same 24h performance",

        # ── /convert ──────────────────────────────────────────
        'convert_step1':       "💱 <b>Convert</b>\n\nStep 1 of 3 - Pick the coin to convert <b>from</b>:",
        'convert_step2':       "💱 <b>Convert</b>\n\n✅ From: <b>{sym}</b>\n\nStep 2 of 3 - Pick the coin to convert <b>to</b>:",
        'convert_step3':       "💱 <b>Convert</b>\n\n✅ From: <b>{from_sym}</b>\n✅ To: <b>{to_sym}</b>\n\nStep 3 of 3 - How much <b>{from_sym}</b>?\n<i>e.g. 10</i>\n\nType /cancel to abort.",
        'convert_result':      "💱 <b>{amount} {from_sym}</b>  →  <b>{result} {to_sym}</b>",
        'convert_fail':        "❌ Conversion failed: {err}",
        'inline_conv_header':  "💱 Conversion",
        'btn_cvt_cancel':      "❌ Cancel",

        # ── /alert ────────────────────────────────────────────
        'alert_step1':         "🔔 <b>Set Price Alert</b>\n\nStep 1 of 3 - Pick a coin:",
        'alert_step2':         "🔔 <b>Set Price Alert</b>\n\n✅ Coin: <b>{sym}</b>  (now {price})\n\nStep 2 of 3 - Notify me when price:",
        'alert_step3':         "🔔 <b>Set Price Alert</b>\n\n✅ Coin: <b>{sym}</b>  (now {price})\n✅ Direction: {arrow} <b>{direction}</b>\n\nStep 3 of 3 - Send the target price:\n<i>e.g. 95000</i>\n\nType /cancel to abort.",
        'alert_set':
            "✅ <b>Alert set!</b>\n\n"
            "🪙 <b>{sym}</b>\n"
            "{arrow} Notify when price goes <b>{direction} {target}</b>\n\n"
            "📍 Current: <b>{current}</b>\n"
            "📏 Distance: {diff}  ({pct}% away)\n\n"
            "<i>You have {count}/{max} alerts active.</i>",
        'alert_limit':         "⚠️ You have {max} active alerts (the maximum).\n\nUse /alerts to remove some first.",
        'alert_invalid_price': "❌ Invalid price. Use a number like <code>95000</code>.",
        'alert_fetch_fail':    "❌ Could not fetch current price. Try again.",
        'alert_bad_direction': "❌ Direction must be <code>above</code> or <code>below</code>.",
        'alert_limit_reached': "⚠️ Alert limit reached ({max} max).\nUse /alerts to remove some.",
        'alert_duplicate':     "⚠️ You already have this alert.\n\nSame coin, price and direction.",
        'btn_above':           "📈 Goes Above",
        'btn_below':           "📉 Goes Below",
        'btn_my_alerts':       "🔔 My Alerts",
        'btn_add_another_alert':"➕ Add Another",
        'btn_add_alert':       "➕ Add Alert",
        'btn_delete_all':      "🗑 Delete All",
        'btn_set_alert':       "🔔 Set an Alert",
        'btn_set_new_alert':   "🔔 Set New Alert",
        'no_alerts':           "🔔 <b>No alerts yet</b>\n\nTap below to set your first price alert.",
        'no_alerts_simple':    "🔔 No alerts yet.\n\nTap below to set your first price alert.",
        'alerts_header':       "🔔 <b>Active Alerts</b>  ({count}/{max})\n",
        'alert_deleted':       "🗑 Alert deleted.",
        'alert_deleted_last':  "🔔 Alert deleted.\n\nNo more active alerts.\nTap below to set a new one.",
        'alerts_all_deleted':  "🔔 All alerts deleted.",
        'above_word':          "Above",
        'below_word':          "Below",
        'alert_triggered':
            "🔔 <b>Alert Triggered!</b>\n\n"
            "{arrow} <b>{sym}</b> hit <b>{price}</b>\n"
            "Your target: {direction} {target}",
        'btn_holdings':        "💼 Holdings",
        'away_pct':            "{pct}% away",

        # ── /digest ───────────────────────────────────────────
        'digest_header':       "📅 <b>Daily Portfolio Digest</b>\n\n",
        'digest_status':       "Status: {status}\nSend time: <b>{hour}:00 Iran</b>\n\nPick a time below, or tap <b>Custom time</b> to enter your own hour (0-23, Iran time).",
        'digest_enabled':      "✅ Enabled",
        'digest_disabled':     "❌ Disabled",
        'btn_enable':          "▶️ Enable",
        'btn_disable':         "⏹ Disable",
        'btn_custom_time':     "🕐 Custom time…",
        'digest_on_toast':     "✅ Daily digest enabled!",
        'digest_off_toast':    "❌ Daily digest disabled.",
        'digest_time_set':     "✅ Time set to {hour}:00",
        'digest_time_prompt': 
            "🕐 What time should I send your daily digest?\n\n"
            "<i>Enter hour (0-23) in <b>Iran time</b></i>\n"
            "<i>Example: <code>9</code> for 9 AM, <code>20</code> for 8 PM</i>\n\n"
            "Use the Cancel button below to abort.",
        'digest_time_confirm': "✅ Digest time set to <b>{hour}:00</b>.",
        'digest_morning':      "☀️ <b>Good morning! Here\'s your portfolio</b>\n",
        'digest_total':        "\n💰 <b>Total: {usd}</b> · {irr} Toman",
        'btn_portfolio':       "💼 Portfolio",
        'btn_alerts':          "🔔 Alerts",

        # ── TRON transaction ──────────────────────────────────
        'tx_header':           "ℹ️ <b>TRON Transaction</b>\n",
        'tx_status':           "{emoji} Status: <b>{status}</b>\n",
        'tx_confirmed':        "Confirmed",
        'tx_failed':           "Failed",
        'tx_pending':          "Pending",
        'tx_block':            "🔗 Block: #{block}\n",
        'tx_time':             "🕐 Time: {time}\n",
        'tx_from':             "📤 From: {addr}\n",
        'tx_to':               "📥 To: {addr}\n",
        'tx_amount':           "💰 Amount: {amount} TRX\n",
        'tx_fee':              "⛽ Fee: {fee} TRX\n",
        'tx_hash':             "📝 Hash: {hash}\n",
        'tx_not_found':        "❌ Transaction not found. Check the hash and try again.",
        'tx_timeout':          "⏳ Request timed out. Try again.",
        'tx_error':            "❌ Could not fetch transaction. Check the hash and try again.",

        # ── inline / misc ─────────────────────────────────────
        'inline_tips_title':   "Crypto Bot - Inline Guide",
        'inline_tips_desc':    "btc · 10trx · usd100 to eth · wallets · hash · gold · currency",
        'inline_tips_body':
            "<b>🪙 Prices & Values</b>\n"
            "• <code>btc</code> - price of any crypto (with USD, Toman, TRY, EUR, GBP, AED, CNY)\n"
            "• <code>10trx</code> or <code>10 trx</code> - value in USD/IRR/TRY/EUR/GBP/AED/CNY\n"
            "• <code>u</code> or <code>10u</code> - USDT alias\n"
            "• <code>100usd to eth</code> or <code>0.5btc to usd</code> - convert\n"
            "• <code>1000toman to usd</code> or <code>10usd to toman</code> - fiat convert\n\n"
            "<b>📊 Market</b>\n"
            "• <code>price</code> or <code>prices</code> - all crypto prices\n"
            "• <code>gold</code> or <code>gold price</code> - XAU/USD\n"
            "• <code>market</code> or <code>fear</code> - market overview & Fear & Greed\n"
            "• <code>star</code> or <code>stars</code> - Telegram Stars price\n"
            "• <code>try</code> or <code>tl</code> - TRY to Toman rate\n"
            "• <code>currency</code> - all fiat rates (USD, EUR, GBP, TRY, AED, CNY, RUB, INR, JPY, KRW)\n\n"
            "<b>💰 Portfolio & Alerts</b>\n"
            "• <code>holdings</code> or <code>portfolio</code> - your portfolio\n"
            "• <code>alerts</code> or <code>my alerts</code> - active price alerts\n"
            "• <code>wallets</code> - wallets with balance\n"
            "• <code>wallets addr</code> - wallet addresses only\n\n"
            "<b>🔗 Blockchain</b>\n"
            "• Paste TRON tx hash or Tronscan link - TX details\n"
            "• Paste TON tx hash or tonviewer/tonscan link - TX details\n"
            "• Paste TRON wallet address (34 chars) - balance\n"
            "• Paste TON wallet address (48 chars, EQ/UQ) - balance\n\n"
            "<b>🧮 Tools</b>\n"
            "• <code>10+20*3</code> or <code>(5+3)*2</code> - calculator\n"
            "• <code>15% of 200</code>  |  <code>100+5%</code>  |  <code>80-20%</code> - percentages\n"
            "• <code>usd</code> - USD to Toman rate",
        'inline_help_button':  "❓ Help",
        'all_prices_title':    "All Crypto Prices",
        'all_prices_tap':      "Tap to share full price list",
        'no_wallets_inline':   "⚠️ No wallets saved yet.\nTap ➕ Add Wallet below.",
        'no_wallets_inline_title': "No wallets saved",
        'no_wallets_inline_desc':  "Add wallet via /wallets",
        'wallet_label':            "Wallet {n}",
        'wallet_addr_only_label':  "Address only",
        'wallet_addr_only_desc':   "Tap to share address",
        'wallet_with_balance_label': "With balance",
        'alert_state_error':   "❌ Something went wrong. Please try /alert again.",
        'invalid_data':        "Invalid data.",
        'all_wallets_removed': "✅ All wallets removed.",
        'no_wallets_to_remove':"⚠️ No wallets saved yet.",
        'gdpr_cancelled':      "Cancelled - your data is safe.",

        # ── Currency / price display ──────────────────────────
        'toman_label':         "Toman",
        'convert_toman_note':  "\n💰 {irr} Toman",
        'convert_toman_approx':"\n💰 ≈ {usd} · {irr} Toman",
        'price_toman_line':    "💰 {irr} Toman",
        'price_try_line':      "💰 {val} TRY",
        'price_eur_line':      "💰 {val} EUR",

        # ── /setcurrency ─────────────────────────────────────
        'cur_current':         "💱 Current display currency: <b>{name}</b>\n\nPick the currency shown under every price:",
        'cur_set':             "✅ Default display currency set to <b>{name}</b>",
        'cur_unknown':         "❌ Unknown currency. Available: <code>{list}</code>",

        # ── /clearwallets ────────────────────────────────────
        'clearwallets_prompt': "⚠️ <b>Remove all saved wallets?</b>\n\nThis deletes every TRON &amp; TON address you saved.\nTheir balances are NOT affected — only this bot forgets them.\n\nThis cannot be undone.",
        'clearwallets_done':   "✅ <b>All saved wallets were removed.</b>",
        'btn_clear_wallets':   "🗑 Yes, remove all",
        'inline_usd_toman':    "USD → Toman",
        'inline_toman_usd':    "Toman → USD",
        'usd_simple':          "1 USD = {rate} Toman",
        'usd_amount':          "{amount} USD = {rate} Toman",

        # ── Exchange / Chart / Trending ───────────────────────
        'exch_set':            "✅ Default price source set to <b>{name}</b>",
        'exch_unknown':        "❌ Unknown exchange. Choose: {list}",
        'exch_usage':          "Usage: /setexchange binance\nOr add @exchange to any query: btc @binance",
        'exch_prompt':         "💱 <b>Select your default price source:</b>",
        'exch_current':        "💱 <b>Current price source:</b> {name}",
        'chart_usage':         "Usage:\n/chart btc\n/chart btc 7d\n/chart btc 90d\n/chart (pick from buttons)",
        'chart_pick_coin':     "📊 <b>Chart</b>\n\nPick a coin:",
        'btn_style_line':      "📊 Line",
        'btn_style_candles':   "🕯 Candles",
        'chart_candle_style':  "🕯 Candles",
        'chart_line_style':    "📊 Line",
        'chart_ohlc':          "O {o} · H {h} · L {l} · C {c}\n🔁 Vol {v} {sym}",
        'chart_pick_days':     "📊 <b>{sym}</b>\n\nPick a time range:",
        'chart_fail':          "❌ Chart generation failed. Try again later.",
        'stars_calc_btn':      "🧮 Calculate amount",
        'stars_prompt':        "⭐ Send the number of <b>stars</b> you want to price:",
        'stars_header':        "⭐ <b>Telegram Stars</b>\n",
        'stars_result':        "⭐ <b>{amount} Stars</b>",
        'stars_invalid':       "❌ Invalid number. Send a positive number like <code>100</code>.",
        'compare_usage':       "Usage: /compare btc eth\n/compare btc sol 90d",
        'compare_fail':        "❌ Comparison failed. Try again later.",
        'trending_header':     "🔥 <b>Trending on CoinGecko</b>\n",
        'trending_fail':       "❌ Could not fetch trending data. Try again later.",
        'gainers_header':      "📈 <b>Top Gainers (24h)</b>\n",
        'losers_header':       "📉 <b>Top Losers (24h)</b>\n",
        'movers_fail':         "❌ Could not fetch market data. Try again later.",
    },

    'fa': {
        # ── Generic ───────────────────────────────────────────
        'cancelled':           "✅ لغو شد.",
        'nothing_to_cancel':   "ℹ️ چیزی برای لغو وجود ندارد.",
        'something_went_wrong':"❌ مشکلی پیش آمد. لطفاً دوباره تلاش کنید.",
        'slow_down':           "⏳ کمی آهسته‌تر! لطفاً حدود {secs} ثانیه دیگر دوباره امتحان کنید.",
        'unknown_coin':        "❌ ارز ناشناخته: <code>{sym}</code>\n\nمثال: BTC, ETH, TRX, SOL…",
        'price_unavailable':   "⚠️ قیمت‌ها در حال حاضر در دسترس نیستند. لطفاً دوباره امتحان کنید.",
        'market_unavailable':  "❌ اطلاعات بازار در دسترس نیست. بعداً امتحان کنید.",
        'invalid_amount':      "❌ مقدار نامعتبر. یک عدد مثبت مثل <code>0.5</code> بفرستید.",
        'invalid_price':       "❌ قیمت نامعتبر. یک عدد مثبت بفرستید.",
        'invalid_expression':  "❌ عبارت نامعتبر.",
        'division_by_zero':    "❌ تقسیم بر صفر.",
        'math_result':         "✅ {expr} = {result}",
        'invalid_hour':        "❌ لطفاً عددی بین ۰ تا ۲۳ بفرستید.",
        'price_fetch_fail':    "❌ در حال حاضر امکان دریافت قیمت وجود ندارد. کمی بعد امتحان کنید.",
        'refreshing':          "در حال بروزرسانی…",
        'refresh_cooldown':    "⏳ {sec} ثانیه صبر کنید.",
        'refresh_limit_reached': "⚠️ محدودیت بروزرسانی. بعداً تلاش کنید.",
        'generating_chart':    "در حال ساخت نمودار…",
        'fetching':            "در حال دریافت…",

        # ── Loading placeholders (⏳ + italic, used everywhere) ──
        'loading_prices':      "⏳ <i>در حال دریافت قیمت‌ها…</i>",
        'loading_chart':       "⏳ <i>در حال ساخت نمودار…</i>",
        'loading_portfolio':   "⏳ <i>در حال ساخت نمودار پرتفو…</i>",
        'loading_market':      "⏳ <i>در حال دریافت داده بازار…</i>",
        'loading_compare':     "⏳ <i>در حال مقایسه ارزها…</i>",

        # ── Language selection ────────────────────────────────
        'lang_prompt':         "🌐 <b>Choose your language</b>\nزبان خود را انتخاب کنید:",
        'lang_set_en':         "✅ Language set to <b>English</b>.",
        'lang_set_fa':         "✅ زبان به <b>فارسی</b> تغییر یافت.",

        # ── /start ────────────────────────────────────────────
        'start_welcome':
            "🌍 <b>{name} عزیز، به Earth Crypto خوش آمدید! 🚀</b>\n"
            "<i>قیمت · نمودار · پرتفو · هشدار · کیف پول</i>\n\n"
            "📈 <b>قیمت‌ها و نمودار</b>\n"
            "<blockquote><code>/price</code> — قیمت همه ارزها با تغییر ۲۴ ساعته\n"
            "<code>/chart</code> — نمودار قیمت (انتخاب ارز و بازه با دکمه)\n"
            "<code>/compare</code> — مقایسه دو ارز (با دکمه)\n"
            "<code>/gold</code> — قیمت طلا (جهانی + ایران)\n"
            "<code>/market</code> — نمای کلی ترس و طمع\n"
            "<code>/trending</code> — ۱۰ ارز داغ\n"
            "<code>/gainers</code> — بیشترین رشد در ۲۴ ساعت\n"
            "<code>/losers</code> — بیشترین افت در ۲۴ ساعت</blockquote>\n\n"
            "💱 <b>نرخ ارز و تبدیل</b>\n"
            "<blockquote><code>/usd</code> — دلار به تومان\n"
            "<code>/convert</code> — مبدل ارز · دلار · تومان\n"
            "<code>/star</code> — قیمت ستاره تلگرام\n"
            "<code>/setcurrency</code> — ارز نمایش پیش‌فرض\n"
            "<code>/setexchange</code> — منبع قیمت پیش‌فرض</blockquote>\n\n"
            "💼 <b>پرتفو · هشدار · کیف پول</b>\n"
            "<blockquote><code>/set</code> — ثبت دارایی‌های شما\n"
            "<code>/holdings</code> — خلاصه سود/زیان پرتفو\n"
            "<code>/digest</code> — تنظیمات خلاصه روزانه\n"
            "<code>/alert</code> — تنظیم هشدار قیمت جدید\n"
            "<code>/alerts</code> — مدیریت هشدارهای شما\n"
            "<code>/wallets</code> — مدیریت آدرس‌های ترون و تون\n"
            "<code>/mywallets</code> — موجودی زنده کیف پول‌ها\n"
            "<code>/clearwallets</code> — حذف همه کیف پول‌ها</blockquote>\n\n"
            "⚙️ <b>تنظیمات و موارد دیگر</b>\n"
            "<blockquote><code>/language</code> — تغییر زبان (English / فارسی)\n"
            "<code>/suggest</code> — ارسال پیشنهاد یا گزارش باگ\n"
            "<code>/donate</code> — حمایت از پروژه\n"
            "<code>/privacy</code> — حریم خصوصی و داده‌ها\n"
            "<code>/deleteaccount</code> — حذف همه داده‌های شما</blockquote>\n\n"
            "⚡ <b>فقط تایپ کنید:</b> <code>btc</code>، <code>10 eth</code>، <code>100 usd</code>\n\n"
            "💖 /donate · 🔒 /privacy · 🌐 /language\n"
            "<i>اینلاین:</i> <code>{bot_username}</code> در هر چتی 🚀",

        # ── /language ─────────────────────────────────────────
        'btn_english':         "🇬🇧 English",
        'btn_persian':         "🇮🇷 فارسی",

        # ── /suggest, /donate ──────────────────────────────────
        'suggest_prompt':
            "💡 <b>ارسال پیشنهاد، گزارش باگ یا بازخورد</b>\n\n"
            "پیام شما به صاحب ربات ارسال می‌شود. برای لغو /cancel را بزنید.",
        'suggest_sent':        "✅ پیشنهاد شما ارسال شد. ممنون!",
        'suggest_fail':        "❌ ارسال پیشنهاد ناموفق بود. دوباره تلاش کنید.",
        'suggest_no_config':   "سیستم پیشنهادات پیکربندی نشده است.",
        'donate_info':
            "💖 <b>حمایت از Earth Crypto</b>\n\n"
            "اگر این ربات برای شما مفید است، از پروژه حمایت کنید!",
        'donate_no_config':    "اطلاعات حمایت مالی پیکربندی نشده است.",

        # ── /privacy ──────────────────────────────────────────
        'privacy_text':
            "🔒 <b>حریم خصوصی و داده‌ها</b>\n\n"
            "Earth Crypto اطلاعات زیر را مرتبط با شناسه تلگرام شما ذخیره می‌کند:\n\n"
            "• <b>دارایی‌ها</b> - مقادیر ارز که از طریق /set یا /holdings وارد کردید\n"
            "• <b>قیمت خرید</b> - میانگین قیمت خرید برای محاسبه سود/زیان\n"
            "• <b>کیف پول‌ها</b> - آدرس‌های ترون و تون که اضافه کردید\n"
            "• <b>هشدارها</b> - اهداف قیمتی که تنظیم کردید\n"
            "• <b>تنظیمات خلاصه</b> - وضعیت و زمان خلاصه روزانه\n"
            "• <b>زبان</b> - زبان انتخابی شما\n\n"
            "هیچ نام، پیام یا اطلاعات شخصی دیگری ذخیره نمی‌شود.\n\n"
            "داده‌های شما روی سرور این ربات ذخیره می‌شوند و هرگز به اشخاص ثالث فروخته یا منتقل نمی‌شوند.\n\n"
            "برای حذف دائمی همه داده‌هایتان از دستور زیر استفاده کنید:\n"
            "<code>/deleteaccount</code>",

        # ── /deleteaccount ────────────────────────────────────
        'delete_confirm_prompt':
            "⚠️ <b>همه داده‌هایتان حذف شود؟</b>\n\n"
            "این عمل به طور دائم حذف می‌کند:\n"
            "• همه دارایی‌ها و قیمت‌های خرید\n"
            "• همه آدرس‌های کیف پول\n"
            "• همه هشدارهای قیمت\n"
            "• تنظیمات خلاصه روزانه\n"
            "• تنظیمات زبان\n"
            "• ارز نمایش پیش‌فرض\n\n"
            "این عمل قابل بازگشت نیست.",
        'btn_delete_yes':      "🗑 بله، همه چیز را حذف کن",
        'btn_cancel':          "❌ لغو",
        'delete_done':
            "✅ <b>همه داده‌های شما حذف شد.</b>\n\n"
            "هر زمان می‌توانید با /start از نو شروع کنید.",

        # ── Wallets ───────────────────────────────────────────
        'no_wallets':          "⚠️ هنوز کیف پولی ذخیره نشده.\n\nروی ➕ ضربه بزنید.",
        'wallets_header':      "👛 <b>کیف پول‌های ذخیره‌شده</b>\n",
        'wallet_added':        "✅ <b>کیف پول اضافه شد!</b>\n\n<code>{address}</code>\n\nشما {count}/{max} کیف پول دارید.",
        'wallet_limit':        "⚠️ به حداکثر تعداد کیف پول ({max}) رسیدید.\nاول یکی را با /wallets حذف کنید.",
        'wallet_invalid':      "❌ <b>آدرس نامعتبر</b>\n\n<code>{address}</code>\n\nفرمت‌های پشتیبانی‌شده:\n• <b>ترون</b> — با <b>T</b> شروع می‌شود، ۳۴ کاراکتر\n• <b>تون</b> — با <b>EQ</b> یا <b>UQ</b> شروع می‌شود، ۴۸ کاراکتر\n\nلطفاً دوباره بررسی کنید.",
        'wallet_already_saved':"⚠️ این کیف پول قبلاً ذخیره شده.",
        'wallet_not_found':    "کیف پول پیدا نشد - لیست ممکن است تغییر کرده باشد.",
        'wallet_header_tron':  "👛 <b>کیف پول ترون</b>\n",
        'wallet_header_ton':   "👛 <b>کیف پول تون</b>\n",
        'wallet_removed_toast':"✅ کیف پول حذف شد.",
        'no_wallets_yet':      "⚠️ هنوز کیف پولی ذخیره نشده.\nاز /wallets برای مدیریت استفاده کنید.",
        'wallets_balances_hdr':"👛 <b>کیف پول‌ها و موجودی</b>\n\n",
        'btn_view_wallets':    "👁 مشاهده کیف پول‌ها",
        'btn_add_wallet':      "➕ افزودن کیف پول",
        'btn_close':           "❌ بستن",
        'btn_remove':          "🗑 حذف",
        'send_wallet_addr':    "🔗 آدرس کیف پول ترون یا تون خود را بفرستید:\n(یا /cancel برای لغو)",
        'no_balance':          "❌ موجودی برای این آدرس یافت نشد.",
        'trx_balance':         "👛 موجودی TRX: {bal} TRX",
        'tron_timeout':        "⏳ درخواست منقضی شد. دوباره امتحان کنید.",
        'tron_error':          "❌ اتصال به شبکه ترون ممکن نیست. دوباره امتحان کنید.",
        'invalid_tron_addr':   "❌ این آدرس ترون معتبر نیست. لطفاً بررسی کنید و دوباره امتحان کنید.",
        'invalid_address':     "❌ آدرس نامعتبر. لطفاً بررسی کنید و دوباره امتحان کنید.",

        # ── Holdings ──────────────────────────────────────────
        'no_holdings':         "⚠️ هنوز دارایی‌ای ثبت نشده.\n\nروی ➕ ضربه بزنید.",
        'portfolio_header':    "💼 <b>پرتفوی شما</b>\n",
        'price_unavail_short': "<i>قیمت موجود نیست</i>",
        'buy_at':              "خرید@{price}",
        'chart_title':         "ترکیب پرتفو  (جمع: ${total})",
        'chart_caption':       "📊 <b>ترکیب پرتفو</b>",
        'price_chart_title':   "نمودار قیمت {sym} ({days} روز اخیر)",
        'price_chart_xlabel':  "تاریخ",
        'price_chart_ylabel':  "قیمت (دلار)",
        'portfolio_total':     "\n💰 <b>جمع: {usd}</b> · {irr} تومان",
        'portfolio_total_cur': "\n💰 <b>جمع: {usd}</b> · {val} {cur}",
        'holding_set':         "✅ <b>{sym}</b> روی <b>{amount}</b> تنظیم شد\n\n",
        'holding_added':       "✅ <b>{amount}</b> {sym} اضافه شد (جمع: <b>{total}</b>)\n\n",
        'btn_add_coin':        "➕ افزودن ارز",
        'btn_add_another':     "➕ افزودن ارز دیگر",
        'btn_chart':           "📊 نمودار",
        'btn_clear_all':       "🗑 پاک کردن همه",
        'btn_edit':            "✏️ ویرایش",
        'btn_buy_price':       "💲 قیمت خرید",
        'edit_amount_prompt':  "✏️ مقدار جدید برای <b>{sym}</b> را بفرستید:\n(یا /cancel برای لغو)",
        'buy_price_prompt':    "💲 میانگین <b>قیمت خرید (دلار)</b> برای <b>{sym}</b>:\nمثال: <code>85000</code>\n\n(یا /cancel برای لغو)",
        'no_holdings_chart':   "⚠️ هنوز دارایی‌ای برای نمودار وجود ندارد.",
        'chart_fail':          "❌ ساخت نمودار ممکن نشد. مطمئن شوید دارایی‌هایتان قیمت دارند.",
        'clear_all_prompt':    "⚠️ <b>همه</b> دارایی‌ها پاک شوند؟",
        'btn_yes_clear':       "✅ بله، پاک کن",
        'holdings_cleared':    "✅ همه دارایی‌ها پاک شدند.",
        'set_holdings_prompt': "💼 <b>ثبت دارایی‌ها</b>\n\nارزی را برای تنظیم مقدار انتخاب کنید:",
        'add_coin_prompt':     "💼 <b>افزودن ارز</b>\n\nکدام ارز را می‌خواهید اضافه کنید؟",
        'coin_amount_prompt':  "💼 <b>{sym}</b>{price}\n\nچقدر <b>{sym}</b> دارید؟\n<i>مثال: <code>0.5</code></i>\n\nبرای لغو /cancel بزنید.",
        'now_price':           "  (اکنون {price})",
        'unknown_coin_short':  "ارز ناشناخته.",
        'holding_removed_toast': "✅ {sym} حذف شد.",
        'invalid_amount_conv': "❌ مقدار نامعتبر. یک عدد مثل <code>10</code> بفرستید.",

        # ── /price ────────────────────────────────────────────
        'prices_header':       "📊 <b>قیمت لحظه‌ای ارزها</b>  (۲۴ ساعت)\n",
        'btn_refresh':         "🔄 بروزرسانی",
        'updated_at':          "آپدیت در {time}",

        # ── /usd ──────────────────────────────────────────────
        'usd_rate':            "<blockquote>💵 <b>1 دلار = {rate} تومان</b></blockquote>",
        'usd_conversion':      "<blockquote>💵 <b>{amount} دلار = {result} تومان</b></blockquote>",
        'rate_unavailable':    "⚠️ نرخ ارز در دسترس نیست.\n\nکمی بعد دوباره امتحان کنید.",

        # ── /try (لیر ترکیه) ──────────────────────────────────
        'try_rate':            "<blockquote>🇹🇷 <b>1 لیر = {rate} تومان</b></blockquote>",
        'try_conversion':      "<blockquote>🇹🇷 <b>{amount} لیر = {result} تومان</b></blockquote>",

        # ── /eur /gbp /aed /cny (نرخ ارز) ─────────────────────
        'eur_rate':            "<blockquote>🇪🇺 <b>1 یورو = {rate} تومان</b></blockquote>",
        'eur_conversion':      "<blockquote>🇪🇺 <b>{amount} یورو = {result} تومان</b></blockquote>",
        'gbp_rate':            "<blockquote>🇬🇧 <b>1 پوند = {rate} تومان</b></blockquote>",
        'gbp_conversion':      "<blockquote>🇬🇧 <b>{amount} پوند = {result} تومان</b></blockquote>",
        'aed_rate':            "<blockquote>🇦🇪 <b>1 درهم = {rate} تومان</b></blockquote>",
        'aed_conversion':      "<blockquote>🇦🇪 <b>{amount} درهم = {result} تومان</b></blockquote>",
        'cny_rate':            "<blockquote>🇨🇳 <b>1 یوآن = {rate} تومان</b></blockquote>",
        'cny_conversion':      "<blockquote>🇨🇳 <b>{amount} یوآن = {result} تومان</b></blockquote>",

        # ── /gold (طلا) ───────────────────────────────────────
        'gold_hdr':            "🥇 <b>طلا</b>\n",
        'gold_global':         "🌍 <i>جهانی · XAU/USD</i>\n💵 اونس: <b>${xau}</b>\n💵 گرم: <b>${gram}</b>",
        'gold_iran':           "🇮🇷 <b>ایران · تومان</b>\n",
        'gold_conv_hdr':       "🥇 <b>{amount} گرم</b>\n",
        'gold_bahar':          "🪙 بهار آزادی: {price}\n",
        'gold_emami':          "🪙 امامی: {price}\n",
        'gold_nim':            "🪙 نیم: {price}\n",
        'gold_rob':            "🪙 ربع: {price}\n",
        'gold_gram18':         "⚖️ گرم 18: {price}\n",
        'gold_gram24':         "⚖️ گرم 24: {price}\n",
        'gold_mesghal':        "⚖️ مثقال: {price}\n",
        'gold_fetch_fail':     "❌ قیمت طلا دریافت نشد. دوباره تلاش کنید.",

        # ── /market ───────────────────────────────────────────
        'market_header':       "🌍 <b>بازار کریپتو</b>\n",
        'market_mcap':         "💹 ارزش بازار: <b>{mcap}</b>  {arrow} {chg}% (۲۴ ساعت)\n",
        'market_vol':          "📊 حجم ۲۴ ساعته: <b>{vol}</b>\n",
        'market_dom':          "🟠 سهم BTC: <b>{btc}%</b>  🔵 سهم ETH: <b>{eth}%</b>\n",
        'market_coins':        "🪙 ارزهای فعال: <b>{coins}</b>\n",
        'market_fg':           "😨 <b>شاخص ترس و طمع</b>\n{fg}",
        'fg_unavailable':      "در دسترس نیست",
        'fg_extreme_fear':     "ترس شدید",
        'fg_fear':             "ترس",
        'fg_neutral':          "خنثی",
        'fg_greed':            "طمع",
        'fg_extreme_greed':    "طمع شدید",

        # ── /compare ──────────────────────────────────────────
        'compare_header':      "📊 <b>مقایسه</b>\n",
        'compare_pick1':       "📊 <b>مقایسه ارزها</b>\n\nارز <b>اول</b> را انتخاب کنید:",
        'compare_pick2':       "📊 <b>مقایسه ارزها</b>\n\n✅ اول: <b>{sym}</b>\n\nحالا ارز <b>دوم</b> را انتخاب کنید:",
        'compare_vol':         "📦 حجم ۲۴س: {vol}\n",
        'compare_mcap':        "💹 ارزش بازار: {mcap}",
        'compare_winner':      "\n🏆 <b>{name}</b> در ۲۴ ساعت گذشته بهتر عمل کرد",
        'compare_tied':        "\n🤝 <b>مساوی</b> - عملکرد یکسان در ۲۴ ساعت",

        # ── /convert ──────────────────────────────────────────
        'convert_step1':       "💱 <b>تبدیل ارز</b>\n\nمرحله ۱ از ۳ - ارز مبدا را انتخاب کنید:",
        'convert_step2':       "💱 <b>تبدیل ارز</b>\n\n✅ از: <b>{sym}</b>\n\nمرحله ۲ از ۳ - ارز مقصد را انتخاب کنید:",
        'convert_step3':       "💱 <b>تبدیل ارز</b>\n\n✅ از: <b>{from_sym}</b>\n✅ به: <b>{to_sym}</b>\n\nمرحله ۳ از ۳ - چقدر <b>{from_sym}</b>؟\n<i>مثال: 10</i>\n\nبرای لغو /cancel بزنید.",
        'convert_result':      "💱 <b>{amount} {from_sym}</b>  →  <b>{result} {to_sym}</b>",
        'convert_fail':        "❌ خطا در تبدیل: {err}",
        'inline_conv_header':  "💱 تبدیل ارز",
        'btn_cvt_cancel':      "❌ لغو",

        # ── /alert ────────────────────────────────────────────
        'alert_step1':         "🔔 <b>تنظیم هشدار قیمت</b>\n\nمرحله ۱ از ۳ - ارز را انتخاب کنید:",
        'alert_step2':         "🔔 <b>تنظیم هشدار قیمت</b>\n\n✅ ارز: <b>{sym}</b>  (اکنون {price})\n\nمرحله ۲ از ۳ - هشدار بده وقتی قیمت:",
        'alert_step3':         "🔔 <b>تنظیم هشدار قیمت</b>\n\n✅ ارز: <b>{sym}</b>  (اکنون {price})\n✅ جهت: {arrow} <b>{direction}</b>\n\nمرحله ۳ از ۳ - قیمت هدف را بفرستید:\n<i>مثال: 95000</i>\n\nبرای لغو /cancel بزنید.",
        'alert_set':
            "✅ <b>هشدار تنظیم شد!</b>\n\n"
            "🪙 <b>{sym}</b>\n"
            "{arrow} هشدار وقتی قیمت <b>{direction} {target}</b>\n\n"
            "📍 قیمت فعلی: <b>{current}</b>\n"
            "📏 فاصله: {diff}  ({pct}% فاصله دارد)\n\n"
            "<i>شما {count}/{max} هشدار فعال دارید.</i>",
        'alert_limit':         "⚠️ شما {max} هشدار فعال دارید (حداکثر).\n\nاول از /alerts چندتا را حذف کنید.",
        'alert_invalid_price': "❌ قیمت نامعتبر. عددی مثل <code>95000</code> بفرستید.",
        'alert_fetch_fail':    "❌ دریافت قیمت فعلی ممکن نشد. دوباره امتحان کنید.",
        'alert_bad_direction': "❌ جهت باید <code>above</code> یا <code>below</code> باشد.",
        'alert_limit_reached': "⚠️ به حداکثر هشدار ({max}) رسیدید.\nاز /alerts چندتا را حذف کنید.",
        'alert_duplicate':     "⚠️ این هشدار قبلاً ثبت شده.\n\nهمان ارز، قیمت و جهت.",
        'btn_above':           "📈 بالاتر برود",
        'btn_below':           "📉 پایین‌تر برود",
        'btn_my_alerts':       "🔔 هشدارهای من",
        'btn_add_another_alert':"➕ افزودن هشدار دیگر",
        'btn_add_alert':       "➕ افزودن هشدار",
        'btn_delete_all':      "🗑 حذف همه",
        'btn_set_alert':       "🔔 تنظیم هشدار",
        'btn_set_new_alert':   "🔔 هشدار جدید",
        'no_alerts':           "🔔 <b>هنوز هشداری ندارید</b>\n\nبرای تنظیم اولین هشدار ضربه بزنید.",
        'no_alerts_simple':    "🔔 هنوز هشداری ندارید.\n\nبرای تنظیم هشدار ضربه بزنید.",
        'alerts_header':       "🔔 <b>هشدارهای فعال</b>  ({count}/{max})\n",
        'alert_deleted':       "🗑 هشدار حذف شد.",
        'alert_deleted_last':  "🔔 هشدار حذف شد.\n\nهیچ هشدار فعالی وجود ندارد.\nبرای تنظیم هشدار جدید ضربه بزنید.",
        'alerts_all_deleted':  "🔔 همه هشدارها حذف شدند.",
        'above_word':          "بالای",
        'below_word':          "زیر",
        'alert_triggered':
            "🔔 <b>هشدار فعال شد!</b>\n\n"
            "{arrow} <b>{sym}</b> به <b>{price}</b> رسید\n"
            "هدف شما: {direction} {target}",
        'btn_holdings':        "💼 پرتفو",
        'away_pct':            "{pct}% فاصله",

        # ── /digest ───────────────────────────────────────────
        'digest_header':       "📅 <b>خلاصه روزانه پرتفو</b>\n\n",
        'digest_status':       "وضعیت: {status}\nزمان ارسال: <b>{hour}:00 ایران</b>\n\nیک زمان انتخاب کنید یا روی <b>زمان دلخواه</b> ضربه بزنید (۰-۲۳، وقت ایران).",
        'digest_enabled':      "✅ فعال",
        'digest_disabled':     "❌ غیرفعال",
        'btn_enable':          "▶️ فعال کردن",
        'btn_disable':         "⏹ غیرفعال کردن",
        'btn_custom_time':     "🕐 زمان دلخواه…",
        'digest_on_toast':     "✅ خلاصه روزانه فعال شد!",
        'digest_off_toast':    "❌ خلاصه روزانه غیرفعال شد.",
        'digest_time_set':     "✅ زمان روی {hour}:00 تنظیم شد",
        'digest_time_prompt':
        "🕐 <b>زمان دلخواه</b>\n\n"
        "ساعت مورد نظر خود را بفرستید (۰ تا ۲۳، وقت ایران):\n"
        "<i>مثال: <code>9</code> برای ۹ صبح، <code>20</code> برای ۸ شب</i>\n\n"
        "از دکمه لغو زیر برای انصراف استفاده کنید.",
        'digest_time_confirm': "✅ زمان خلاصه روی <b>{hour}:00</b> تنظیم شد.",
        'digest_morning':      "☀️ <b>صبح بخیر! پرتفوی شما</b>\n",
        'digest_total':        "\n💰 <b>جمع: {usd}</b> · {irr} تومان",
        'btn_portfolio':       "💼 پرتفو",
        'btn_alerts':          "🔔 هشدارها",

# ── TRON transaction ──────────────────────────────────
        'tx_header':           "ℹ️ <b>تراکنش ترون</b>\n",
        'tx_status':           "{emoji} وضعیت: <b>{status}</b>\n",
        'tx_confirmed':        "تأیید شده",
        'tx_failed':           "ناموفق",
        'tx_pending':          "در انتظار",
        'tx_block':            "🔗 بلوک: #{block}\n",
        'tx_time':             "🕐 زمان: {time}\n",
        'tx_from':             "📤 فرستنده: {addr}\n",
        'tx_to':               "📥 گیرنده: {addr}\n",
        'tx_amount':           "💰 مقدار: {amount} TRX\n",
        'tx_fee':              "⛽ کارمزد: {fee} TRX\n",
        'tx_hash':             "📝 هش: {hash}\n",
        'tx_not_found':        "❌ تراکنش پیدا نشد. هش را بررسی کنید.",
        'tx_timeout':          "⏳ درخواست منقضی شد. دوباره امتحان کنید.",
        'tx_error':            "❌ دریافت تراکنش ممکن نشد. هش را بررسی کنید.",

        # ── inline / misc ─────────────────────────────────────
        'inline_tips_title':   "ربات کریپتو - راهنمای اینلاین",
        'inline_tips_desc':    "btc · 10trx · usd100 to eth · ولت · hash · طلا · ارز",
        'inline_tips_body':
            "<b>🪙 قیمت‌ها و ارزش</b>\n"
            "• <code>btc</code> - قیمت هر ارز (دلار، تومان، لیر، یورو، پوند، درهم، یوان)\n"
            "• <code>10trx</code> یا <code>10 trx</code> - ارزش به دلار/تومان/لیر/یورو/پوند/درهم/یوان\n"
            "• <code>u</code> یا <code>10u</code> - USDT\n"
            "• <code>100usd to eth</code> یا <code>0.5btc to usd</code> - تبدیل\n"
            "• <code>1000تومان to usd</code> یا <code>10usd to تومان</code> - تبدیل ارز\n\n"
            "<b>📊 بازار</b>\n"
            "• <code>price</code> یا <code>قیمت</code> - قیمت همه ارزها\n"
            "• <code>gold</code> یا <code>طلا</code> - قیمت طلا (XAU/USD)\n"
            "• <code>market</code> یا <code>بازار</code> - نمای کلی بازار و ترس و طمع\n"
            "• <code>star</code> یا <code>استارز</code> - قیمت Telegram Stars\n"
            "• <code>try</code> یا <code>لیر</code> - نرخ لیر ترکیه به تومان\n"
            "• <code>currency</code> یا <code>ارز</code> - همه نرخ‌های ارز (دلار، یورو، پوند، لیر، درهم، یوان، روپیه، یوان، ین، وون)\n\n"
            "<b>💰 پرتفو و هشدارها</b>\n"
            "• <code>holdings</code> یا <code>دارایی</code> - پرتفوی شما\n"
            "• <code>alerts</code> یا <code>هشدارها</code> - هشدارهای قیمتی فعال\n"
            "• <code>ولت</code> - کیف پول‌ها با موجودی\n"
            "• <code>ولت آدرس</code> - فقط آدرس کیف پول‌ها\n\n"
            "<b>🔗 بلاکچین</b>\n"
            "• لینک تراکنش TRON یا hash - جزئیات تراکنش\n"
            "• لینک تراکنش TON یا hash - جزئیات تراکنش\n"
            "• آدرس کیف پول TRON (34 کاراکتر) - موجودی\n"
            "• آدرس کیف پول TON (48 کاراکتر, EQ/UQ) - موجودی\n\n"
            "<b>🧮 ابزارها</b>\n"
            "• <code>10+20*3</code> یا <code>(5+3)*2</code> - ماشین حساب\n"
            "• <code>15% of 200</code>  |  <code>100+5%</code>  |  <code>80-20%</code> - درصد\n"
            "• <code>usd</code> - نرخ دلار به تومان",
        'inline_help_button':  "❓ راهنما",
        'all_prices_title':    "همه قیمت‌های ارز",
        'all_prices_tap':      "ضربه بزنید تا لیست کامل را به اشتراک بگذارید",
        'no_wallets_inline':   "⚠️ هنوز کیف پولی ذخیره نشده.\nروی ➕ ضربه بزنید.",
        'no_wallets_inline_title': "کیف پولی ذخیره نشده",
        'no_wallets_inline_desc':  "از /wallets اضافه کنید",
        'wallet_label':            "کیف پول {n}",
        'wallet_addr_only_label':  "فقط آدرس",
        'wallet_addr_only_desc':   "برای اشتراک‌گذاری آدرس ضربه بزنید",
        'wallet_with_balance_label': "با موجودی",
        'alert_state_error':   "❌ مشکلی پیش آمد. لطفاً /alert را دوباره امتحان کنید.",
        'invalid_data':        "داده نامعتبر.",
        'all_wallets_removed': "✅ همه کیف پول‌ها حذف شدند.",
        'no_wallets_to_remove':"⚠️ هنوز کیف پولی ذخیره نشده.",
        'gdpr_cancelled':      "لغو شد - داده‌های شما در امان هستند.",

        # ── Currency / price display ──────────────────────────
        'toman_label':         "تومان",
        'convert_toman_note':  "\n💰 {irr} تومان",
        'convert_toman_approx':"\n💰 ≈ {usd} · {irr} تومان",
        'price_toman_line':    "💰 {irr} تومان",
        'price_try_line':      "💰 {val} لیر",
        'price_eur_line':      "💰 {val} یورو",

        # ── /setcurrency ─────────────────────────────────────
        'cur_current':         "💱 ارز نمایش فعلی: <b>{name}</b>\n\nارزی که زیر هر قیمت نمایش داده می‌شود را انتخاب کنید:",
        'cur_set':             "✅ ارز نمایش پیش‌فرض به <b>{name}</b> تغییر یافت",
        'cur_unknown':         "❌ ارز ناشناخته. گزینه‌های معتبر: <code>{list}</code>",

        # ── /clearwallets ────────────────────────────────────
        'clearwallets_prompt': "⚠️ <b>همه کیف پول‌های ذخیره‌شده حذف شوند؟</b>\n\nاین کار همه آدرس‌های ترون و تون ذخیره‌شده را پاک می‌کند.\nموجودی خود کیف پول‌ها تغییری نمی‌کند — فقط این ربات آن‌ها را فراموش می‌کند.\n\nاین عمل قابل بازگشت نیست.",
        'clearwallets_done':   "✅ <b>همه کیف پول‌های ذخیره‌شده حذف شدند.</b>",
        'btn_clear_wallets':   "🗑 بله، همه حذف شوند",
        'inline_usd_toman':    "دلار → تومان",
        'inline_toman_usd':    "تومان → دلار",
        'usd_simple':          "1 دلار = {rate} تومان",
        'usd_amount':          "{amount} دلار = {rate} تومان",

        # ── Exchange / Chart / Trending ───────────────────────
        'exch_set':            "✅ منبع قیمت پیش‌فرض به <b>{name}</b> تغییر یافت",
        'exch_unknown':        "❌ صرافی ناشناخته. انتخاب کنید: {list}",
        'exch_usage':          "نحوه استفاده: /setexchange binance\nیا @exchange به هر query اضافه کنید: btc @binance",
        'exch_prompt':         "💱 <b>منبع قیمت پیش‌فرض را انتخاب کنید:</b>",
        'exch_current':        "💱 <b>منبع قیمت فعلی:</b> {name}",
        'chart_usage':         "نحوه استفاده:\n/chart btc\n/chart btc 7d\n/chart btc 90d\n/chart (انتخاب با دکمه)",
        'chart_pick_coin':     "📊 <b>نمودار</b>\n\nیک ارز را انتخاب کنید:",
        'btn_style_line':      "📊 خطی",
        'btn_style_candles':   "🕯 کندل",
        'chart_candle_style':  "🕯 کندل",
        'chart_line_style':    "📊 خطی",
        'chart_ohlc':          "باز {o} · بیشینه {h} · کمینه {l} · بسته {c}\n🔁 حجم {v} {sym}",
        'chart_pick_days':     "📊 <b>{sym}</b>\n\nبازه زمانی را انتخاب کنید:",
        'chart_fail':          "❌ تولید نمودار ناموفق بود. دوباره تلاش کنید.",
        'stars_calc_btn':      "🧮 محاسبه تعداد",
        'stars_prompt':        "⭐ تعداد <b>ستاره</b> موردنظر را بفرستید:",
        'stars_header':        "⭐ <b>ستاره تلگرام</b>\n",
        'stars_result':        "⭐ <b>{amount} ستاره</b>",
        'stars_invalid':       "❌ عدد نامعتبر. یک عدد مثبت مثل <code>100</code> بفرستید.",
        'compare_usage':       "نحوه استفاده: /compare btc eth\n/compare btc sol 90d",
        'compare_fail':        "❌ مقایسه ناموفق بود. دوباره تلاش کنید.",
        'trending_header':     "🔥 <b>محبوب‌های CoinGecko</b>\n",
        'trending_fail':       "❌ دریافت داده محبوب‌ها ناموفق بود.",
        'gainers_header':      "📈 <b>بیشترین افزایش (24h)</b>\n",
        'losers_header':       "📉 <b>بیشترین کاهش (24h)</b>\n",
        'movers_fail':         "❌ دریافت داده بازار ناموفق بود.",
    }
}

EMOJIS = {
    'rocket': '🚀', 'chart': '📊', 'money': '💰', 'wallet': '👛',
    'check': '✅', 'cross': '❌', 'warning': '⚠️', 'info': 'ℹ️',
    'fire': '🔥', 'star': '⭐', 'gold': '🥇'
}

CRYPTO_LIST = {
    'bitcoin': '🪙 Bitcoin (BTC)',
    'ethereum': '🪙 Ethereum (ETH)',
    'tether': '🪙 Tether (USDT)',
    'binancecoin': '🪙 Binance Coin (BNB)',
    'solana': '🪙 Solana (SOL)',
    'ripple': '🪙 Ripple (XRP)',
    'cardano': '🪙 Cardano (ADA)',
    'dogecoin': '🪙 Dogecoin (DOGE)',
    'tron': '🪙 Tron (TRX)',
    'the-open-network': '🪙 TON (TON)',
    'avalanche-2': '🪙 Avalanche (AVAX)',
    'chainlink': '🪙 Chainlink (LINK)',
    'litecoin': '🪙 Litecoin (LTC)',
    'polkadot': '🪙 Polkadot (DOT)',
    'shiba-inu': '🪙 Shiba Inu (SHIB)',
    'uniswap': '🪙 Uniswap (UNI)',
    'stellar': '🪙 Stellar (XLM)',
    'monero': '🪙 Monero (XMR)',
    'near': '🪙 NEAR Protocol (NEAR)',
    'aptos': '🪙 Aptos (APT)',
    'sui': '🪙 Sui (SUI)',
    'cosmos': '🪙 Cosmos (ATOM)',
    'algorand': '🪙 Algorand (ALGO)',
    'pepe': '🪙 Pepe (PEPE)',
    'telegram-stars': '⭐ Telegram Stars (STARS)'
}

CRYPTO_ALIASES = {
    'btc': 'bitcoin', 'بیتکوین': 'bitcoin', 'بیت کوین': 'bitcoin', 'بیت‌کوین': 'bitcoin', 'bitcoin': 'bitcoin',
    'بیت': 'bitcoin',
    'eth': 'ethereum', 'اتریوم': 'ethereum', 'اتر': 'ethereum', 'ethereum': 'ethereum',
    'usdt': 'tether', 'تتر': 'tether', 'tether': 'tether',
    'bnb': 'binancecoin', 'بایننس': 'binancecoin', 'binance': 'binancecoin',
    'بینانس': 'binancecoin', 'بی ان بی': 'binancecoin',
    'sol': 'solana', 'سولانا': 'solana', 'solana': 'solana',
    'سول': 'solana',
    'xrp': 'ripple', 'ریپل': 'ripple', 'ripple': 'ripple',
    'ریپ': 'ripple', 'ایکس آر پی': 'ripple',
    'ada': 'cardano', 'کاردانو': 'cardano', 'cardano': 'cardano',
    'کاردان': 'cardano', 'آدا': 'cardano',
    'doge': 'dogecoin', 'دوج': 'dogecoin', 'دوج کوین': 'dogecoin', 'dogecoin': 'dogecoin',
    'دوگ': 'dogecoin',
    'trx': 'tron', 'ترون': 'tron', 'tron': 'tron',
    'تی آر ایکس': 'tron',
    'ton': 'the-open-network', 'تون': 'the-open-network', 'toncoin': 'the-open-network', 'gram': 'the-open-network',
    'تانکوین': 'the-open-network',
    'avax': 'avalanche-2', 'آوالانچ': 'avalanche-2', 'avalanche': 'avalanche-2',
    'link': 'chainlink', 'لینک': 'chainlink', 'chainlink': 'chainlink', 'چین لینک': 'chainlink',
    'ltc': 'litecoin', 'لایتکوین': 'litecoin', 'litecoin': 'litecoin', 'لایت': 'litecoin',
    'uni': 'uniswap', 'یونی‌سواپ': 'uniswap', 'uniswap': 'uniswap', 'یونی سواپ': 'uniswap',
    'xlm': 'stellar', 'استلار': 'stellar', 'stellar': 'stellar',
    'xmr': 'monero', 'مونرو': 'monero', 'monero': 'monero',
    'dot': 'polkadot', 'پولکادات': 'polkadot', 'polkadot': 'polkadot',
    'پولکا': 'polkadot', 'دات': 'polkadot',
    'shib': 'shiba-inu', 'شیبا': 'shiba-inu', 'shiba': 'shiba-inu',
    'شیبا اینو': 'shiba-inu',
    'near': 'near', 'نیر': 'near', 'nearprotocol': 'near',
    'apt': 'aptos', 'آپتوس': 'aptos', 'aptos': 'aptos',
    'sui': 'sui', 'سویی': 'sui', 'سوئی': 'sui',
    'atom': 'cosmos', 'کازموس': 'cosmos', 'cosmos': 'cosmos',
    'algo': 'algorand', 'الگورند': 'algorand', 'algorand': 'algorand',
    'pepe': 'pepe', 'پپه': 'pepe',
    'stars': 'telegram-stars', 'star': 'telegram-stars', 'استار': 'telegram-stars',
    'استارز': 'telegram-stars', 'ستاره': 'telegram-stars', 'telegram': 'telegram-stars',
}

FIAT_ALIASES = {
    'usd': 'usd', 'دلار': 'usd', 'dollar': 'usd', 'dollars': 'usd', '$': 'usd',
    'toman': 'toman', 'تومان': 'toman', 'تومن': 'toman', 'irr': 'toman', 'ریال': 'toman',
    'try': 'try', 'tl': 'try', 'lira': 'try', '₺': 'try',
}


# ─────────────────────────────────────────────
# TRON address validation (Base58Check)
# ─────────────────────────────────────────────
BASE58_ALPHABET = b'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def _b58decode(s: str) -> bytes:
    alphabet = BASE58_ALPHABET
    n = 0
    for char in s.encode():
        if char not in alphabet:
            raise ValueError(f"Invalid base58 character: {chr(char)!r}")
        n = n * 58 + alphabet.index(char)
    return n.to_bytes(25, 'big')


def is_valid_tron_address(address: str) -> bool:
    """Validates a TRON address using Base58Check."""
    if not (len(address) == 34 and address.startswith('T')):
        return False
    try:
        decoded = _b58decode(address)
        payload, checksum = decoded[:-4], decoded[-4:]
        digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
        return digest == checksum
    except Exception:
        return False

def is_valid_ton_address(address: str) -> bool:
    """
    Validate TON wallet address.
    TON addresses are 48 characters, base64-like format.
    Format: EQ... or UQ... (48 chars total)
    """
    if not address:
        return False
    
    # TON addresses start with EQ or UQ and are 48 chars
    if len(address) == 48 and address[:2] in ['EQ', 'UQ']:
        # Check if rest is base64-like (alphanumeric + - _)
        rest = address[2:]
        if re.match(r'^[A-Za-z0-9_-]+$', rest):
            return True
    
    return False


def _raw_ton_to_user_friendly(raw_address: str) -> str:
    """
    Convert raw TON address (workchain:hex) to user-friendly format (EQ/UQ...).
    Raw format: '0:38f5db3c0c772023befbd23ea0a8e62d26995c003078d146f57a7e7f41b3edc6'
    User-friendly: 'UQA49ds8DHcgI7770j6gqOYtJplcADB40Ub1en5_QbPtxoFU'

    Known limitation (deliberate, not a bug): a raw API address carries no
    bounceable/non-bounceable flag, so it is impossible to tell whether the
    source string started with EQ or UQ. We always emit the NON-bounceable
    'UQ...' encoding (tag 0x51) because that is what regular wallets display
    for their own accounts. Both encodings refer to the SAME account — only
    the flag byte differs — so balances and transfers are unaffected; the
    string just may not byte-identically match the user's EQ-prefixed copy.
    """
    if not raw_address or ':' not in raw_address:
        return raw_address
    
    try:
        parts = raw_address.split(':')
        if len(parts) != 2:
            return raw_address
        
        workchain = int(parts[0])
        hex_addr = parts[1].lower()
        
        # Ensure hex_addr is 64 chars (32 bytes)
        if len(hex_addr) != 64:
            return raw_address
        
        # Convert hex to bytes
        addr_bytes = bytes.fromhex(hex_addr)
        
        # Tag: 0x51 for non-bounceable (UQ...), 0x11 for bounceable (EQ...)
        # TON Center API typically returns non-bounceable addresses for mainnet
        tag = 0x51
        
        # Workchain as signed byte (but encoded as unsigned for the payload)
        wc_byte = workchain & 0xFF
        
        # Create the 34-byte payload: tag + workchain + address
        payload = bytes([tag, wc_byte]) + addr_bytes
        
        # Compute CRC16-XModem with initial value 0x0000 (TON uses this)
        crc = 0x0000
        for byte in payload:
            crc ^= byte << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ 0x1021
                else:
                    crc = crc << 1
                crc &= 0xFFFF
        
        # Append checksum (2 bytes, big-endian)
        full_bytes = payload + crc.to_bytes(2, 'big')
        
        # Base64url encode
        import base64
        b64 = base64.urlsafe_b64encode(full_bytes).decode().rstrip('=')
        
        return b64
    except Exception:
        return raw_address


_TON_ADDR_RE = re.compile(r'^(?:EQ|UQ)[A-Za-z0-9+/_-]{46}$')
_TRON_ADDR_RE = re.compile(r'^T[1-9A-HJ-NP-Za-km-z]{33}$')


def detect_wallet_chain(address: str) -> str | None:
    """Detect blockchain from wallet address format.
    Returns 'tron', 'ton', or None if unrecognised.

    Character-class validation matters here: these addresses are interpolated
    into API URLs (toncenter/tonapi/tronscan), so an unvalidated "address"
    like 'EQaaaa...&x=1' would let a forged string add query parameters.
    """
    if not address:
        return None
    if _TRON_ADDR_RE.fullmatch(address):
        return 'tron'
    if _TON_ADDR_RE.fullmatch(address):
        return 'ton'
    return None


def get_wallet_balance(address: str, user_id: int = 0) -> str:
    """Fetch balance for any supported wallet address (auto-detects chain)."""
    chain = detect_wallet_chain(address)
    if chain == 'tron':
        return get_tron_wallet_trx(address, user_id)
    elif chain == 'ton':
        return get_ton_wallet_balance(address, user_id)
    return "❌ Unknown wallet type"


def get_ton_wallet_balance(address, user_id: int = 0):
    """
    Fetch TON wallet balance using TON API.
    """
    try:
        # Using TON Center API (public)
        url = f"https://toncenter.com/api/v2/getAddressInformation?address={address}"
        response = session.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('ok') and 'result' in data:
            result = data['result']
            # Balance is in nanotons (1 TON = 1,000,000,000 nanotons)
            balance_nano = int(result.get('balance', 0))
            balance_ton = Decimal(str(balance_nano)) / Decimal('1000000000')
            
            # Get current TON price and exchange rate
            ton_price = get_crypto_price('the-open-network')
            usd_to_irr = get_usd_to_irr()
            user_lang = db_get_lang(user_id)
            
            if not ton_price or usd_to_irr is None:
                bal_str = format_crypto(balance_ton)
                return T(user_id, 'wallet_header_ton') + quote(f"🪙 {bal_str} TON")
            
            # Enhanced display with crypto + USD + Toman
            balance_display = format_wallet_balance(
                crypto_amount=balance_ton,
                crypto_symbol='TON',
                usd_rate=Decimal(str(ton_price)),
                toman_rate=Decimal(str(usd_to_irr)),
                user_lang=user_lang
            )
            
            return T(user_id, 'wallet_header_ton') + quote(balance_display)
        
        return "❌ Could not fetch wallet balance"
        
    except requests.Timeout:
        logger.error(f"Timeout fetching TON wallet: {address}")
        return "⏳ Request timed out. Try again."
    except Exception as e:
        logger.error(f"Error fetching TON wallet: {e}")
        return "❌ Error fetching wallet. Check address."


def get_ton_transaction_details(hash_value, user_id: int = 0):
    """Full verbose format for regular messages."""
    return _format_ton_transaction(hash_value, user_id, inline=False)


def get_ton_transaction_details_inline(hash_value, user_id: int = 0):
    """Compact format for inline query results."""
    return _format_ton_transaction(hash_value, user_id, inline=True)


def _format_ton_transaction(hash_value, user_id: int = 0, inline: bool = False):
    """
    Fetch TON transaction details.
    Primary: TonViewer API
    Secondary: TonScan fallback
    """
    # Try TonViewer API first (most accurate)
    try:
        # Transaction hashes must be plain hex before they may appear in a URL
        if not re.fullmatch(r'[0-9a-fA-F]{64}', str(hash_value)):
            return "❌ Invalid transaction hash."
        url = f"https://tonapi.io/v2/blockchain/transactions/{hash_value}"
        headers = {'accept': 'application/json'}
        response = session.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Parse transaction
        lt = data.get('lt', 'N/A')
        utime = data.get('utime', 0)
        time_str = datetime.fromtimestamp(utime, IRAN_TZ).strftime('%Y-%m-%d %H:%M:%S') if utime else 'N/A'
        
        # Get account info
        account = data.get('account', {})
        account_address = account.get('address', 'N/A')
        
        # Get in_msg (incoming message)
        in_msg = data.get('in_msg', {})
        source = in_msg.get('source', {})
        source_address = source.get('address', 'N/A') if source else 'N/A'
        
        destination = in_msg.get('destination', {})
        dest_address = destination.get('address', 'N/A') if destination else 'N/A'
        
        # Convert raw TON addresses to user-friendly format
        if source_address != 'N/A' and ':' in source_address:
            source_address = _raw_ton_to_user_friendly(source_address)
        if dest_address != 'N/A' and ':' in dest_address:
            dest_address = _raw_ton_to_user_friendly(dest_address)
        
        # Get value
        value_nano = int(in_msg.get('value', 0))
        value_ton = value_nano / 1_000_000_000
        
        # Check transaction status
        # TON transaction status: check if aborted or failed
        aborted = data.get('aborted', False)
        exit_code = data.get('exit_code', 0)
        is_failed = aborted or exit_code != 0
        
        if is_failed:
            status_emoji = '❌'
            status_text = 'Failed'
        else:
            status_emoji = '✅'
            status_text = 'Success'
        
        # Determine if incoming or outgoing from account perspective
        # For the account in data['account']:
        # - Incoming: in_msg.value > 0 and in_msg.destination == account_address
        # - Outgoing: out_msgs[0].value > 0 and out_msgs[0].source == account_address
        is_incoming = value_ton > 0 and dest_address == account_address
        is_outgoing = False
        out_value_ton = 0
        out_dest_address = 'N/A'
        out_source_address = 'N/A'
        
        out_msgs = data.get('out_msgs', [])
        if out_msgs:
            first_out = out_msgs[0]
            out_value_nano = int(first_out.get('value', 0))
            out_value_ton = out_value_nano / 1_000_000_000
            out_source = first_out.get('source', {})
            out_source_address = out_source.get('address', 'N/A') if out_source else 'N/A'
            out_dest = first_out.get('destination', {})
            out_dest_address = out_dest.get('address', 'N/A') if out_dest else 'N/A'
            is_outgoing = out_value_ton > 0 and out_source_address == account_address
        
        # If in_msg doesn't have clear source/dest, try out_msgs for missing info
        if source_address == 'N/A' or dest_address == 'N/A':
            if out_msgs:
                first_out = out_msgs[0]
                if source_address == 'N/A':
                    src = first_out.get('source', {})
                    source_address = src.get('address', account_address) if src else account_address
                if dest_address == 'N/A':
                    dst = first_out.get('destination', {})
                    dest_address = dst.get('address', 'N/A') if dst else 'N/A'
                # Convert raw TON addresses from out_msgs too
                if source_address != 'N/A' and ':' in source_address:
                    source_address = _raw_ton_to_user_friendly(source_address)
                if dest_address != 'N/A' and ':' in dest_address:
                    dest_address = _raw_ton_to_user_friendly(dest_address)
                if value_ton == 0:
                    value_nano = int(first_out.get('value', 0))
                    value_ton = value_nano / 1_000_000_000
        
        # For display: determine the counterpart address (the other party)
        # For incoming: From = source_address (sender), To = account_address (this wallet)
        # For outgoing: From = account_address (this wallet), To = out_dest_address (recipient)
        if is_incoming and not is_outgoing:
            # Pure incoming
            display_from = source_address
            display_to = account_address
            display_value = value_ton
        elif is_outgoing and not is_incoming:
            # Pure outgoing
            display_from = account_address
            display_to = out_dest_address
            display_value = out_value_ton
        elif is_incoming and is_outgoing:
            # Both - this is a swap or complex tx, show both
            display_from = source_address
            display_to = out_dest_address
            display_value = value_ton  # or could show both values
        else:
            # Fallback
            display_from = source_address
            display_to = dest_address
            display_value = value_ton
        
        # For backward compatibility with display logic below
        # We'll use display_from and display_to for the From/To fields
        final_from = display_from
        final_to = display_to
        
        # Build result - escape all dynamic content for safety
        from_escaped = html.escape(str(final_from)) if final_from != 'N/A' else 'N/A'
        to_escaped = html.escape(str(final_to)) if final_to != 'N/A' else 'N/A'
        hash_escaped = html.escape(str(hash_value))
        
        # Use display_value for amount
        final_value = display_value if 'display_value' in locals() else value_ton
        
        # ── Shared body: identical for regular messages and inline results ──
        body = f"{status_emoji} Status: <b>{status_text}</b>\n"
        body += f"🕐 Time: {time_str}\n"
        if final_from != 'N/A':
            body += f"📤 From: <code>{from_escaped}</code>\n"
        if final_to != 'N/A':
            body += f"📥 To: <code>{to_escaped}</code>\n"
        if final_value > 0:
            body += f"💰 Amount: <b>{final_value:.4f} TON</b>\n"
        body += f"📝 Hash: <code>{hash_escaped}</code>"

        return (
            "ℹ️ <b>TON Transaction</b>\n"
            + quote(body)
            + "\n🔗 <a href='https://tonviewer.com/transaction/{0}'>TonViewer</a> · "
              "<a href='https://tonscan.org/tx/{0}'>TonScan</a>".format(hash_escaped)
        )
        
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            # Try TonScan as fallback
            logger.info(f"TonViewer 404, trying TonScan for {hash_value}")
            return _get_ton_tx_fallback(hash_value, user_id, inline)
        logger.error(f"TonViewer HTTP error: {e}")
        return _get_ton_tx_fallback(hash_value, user_id, inline)
    except requests.Timeout:
        logger.error(f"TonViewer timeout for {hash_value}")
        return _get_ton_tx_fallback(hash_value, user_id, inline)
    except Exception as e:
        logger.error(f"TonViewer error: {e}")
        return _get_ton_tx_fallback(hash_value, user_id, inline)


def _get_ton_tx_fallback(hash_value, user_id: int = 0, inline: bool = False):
    """
    Fallback method using TonScan API when TonViewer fails.
    """
    try:
        url = f"https://toncenter.com/api/v3/transactionsByMessage?msg_hash={hash_value}"
        headers = {'accept': 'application/json'}
        response = session.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        transactions = data.get('transactions', [])
        if not transactions:
            return "❌ Transaction not found"
        
        tx = transactions[0]
        
        # Parse transaction
        utime = tx.get('utime', 0)
        time_str = datetime.fromtimestamp(utime, IRAN_TZ).strftime('%Y-%m-%d %H:%M:%S') if utime else 'N/A'
        
        # Get addresses
        account_addr = tx.get('account', 'N/A')
        
        # Get in message
        in_msg = tx.get('in_msg', {})
        source = in_msg.get('source', 'N/A')
        destination = in_msg.get('destination', 'N/A')
        value_nano = int(in_msg.get('value', 0))
        value_ton = value_nano / 1_000_000_000
        
        # Convert raw TON addresses to user-friendly format
        if source != 'N/A' and ':' in source:
            source = _raw_ton_to_user_friendly(source)
        if destination != 'N/A' and ':' in destination:
            destination = _raw_ton_to_user_friendly(destination)
        
        # Check status from TonScan
        # TonScan might have a status field
        status = tx.get('status', 1)  # 1 = success, other = failed
        is_failed = status != 1
        
        if is_failed:
            status_emoji = '❌'
            status_text = 'Failed'
        else:
            status_emoji = '✅'
            status_text = 'Success'
        
        # Build result - escape all dynamic content for safety
        source_escaped = html.escape(str(source)) if source and source != 'N/A' else 'N/A'
        dest_escaped = html.escape(str(destination)) if destination and destination != 'N/A' else 'N/A'
        hash_escaped = html.escape(str(hash_value))
        
        # ── Shared body: identical for regular messages and inline results ──
        body = f"{status_emoji} Status: <b>{status_text}</b>\n"
        body += f"🕐 Time: {time_str}\n"
        if source and source != 'N/A':
            body += f"📤 From: <code>{source_escaped}</code>\n"
        if destination and destination != 'N/A':
            body += f"📥 To: <code>{dest_escaped}</code>\n"
        if value_ton > 0:
            body += f"💰 Amount: <b>{value_ton:.4f} TON</b>\n"
        body += f"📝 Hash: <code>{hash_escaped}</code>"

        return (
            "ℹ️ <b>TON Transaction</b> <i>(TonScan)</i>\n"
            + quote(body)
            + "\n🔗 <a href='https://tonviewer.com/transaction/{0}'>TonViewer</a> · "
              "<a href='https://tonscan.org/tx/{0}'>TonScan</a>".format(hash_escaped)
        )
        
    except Exception as e:
        logger.error(f"TonScan fallback error: {e}")
        hash_escaped = html.escape(str(hash_value))
        return (
            "❌ <b>Couldn't fetch transaction details</b>\n"
            + quote(f"📝 Hash: <code>{hash_escaped}</code>")
            + f"\n🔗 <a href='https://tonviewer.com/transaction/{hash_escaped}'>View on TonViewer</a>"
        )

# ─────────────────────────────────────────────
# API helpers
# ─────────────────────────────────────────────
def get_crypto_price(crypto_id, cache_only=False):
    # Special handling for Telegram Stars (official rate)
    if crypto_id == 'telegram-stars':
        cached = cache_get('telegram-stars')
        if cached is not None:
            return cached
        price = float(os.getenv('STARS_PRICE_USD', '0.015'))
        cache_set('telegram-stars', price)
        return price
    
    cached = cache_get(crypto_id)
    if cached is not None:
        return cached
    if cache_only:
        return None
    
    # 1) CoinGecko
    if not cache_get('cg_rate_limited'):
        try:
            resp = session.get(
                f"https://api.coingecko.com/api/v3/simple/price?ids={crypto_id}&vs_currencies=usd",
                timeout=8
            )
            if resp.status_code == 200:
                data = resp.json()
                if crypto_id in data:
                    price = data[crypto_id]['usd']
                    cache_set(crypto_id, price)
                    return price
            elif resp.status_code == 429:
                cache_set('cg_rate_limited', True, ttl=60)
        except Exception as e:
            logger.debug(f"CoinGecko price fetch failed: {e}")
    
    # 2) Binance
    try:
        sym = EXCHANGE_SYMBOL_MAP.get(crypto_id)
        if sym:
            resp = session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={'symbol': f"{sym}USDT"},
                timeout=8
            )
            if resp.status_code == 200:
                price = float(resp.json().get('price', 0))
                if price:
                    cache_set(crypto_id, price)
                    return price
    except Exception as e:
        logger.debug(f"Binance price fetch failed: {e}")
    
    # 3) CryptoCompare
    try:
        sym = EXCHANGE_SYMBOL_MAP.get(crypto_id)
        if sym:
            resp = session.get(f"https://min-api.cryptocompare.com/data/price?fsym={sym}&tsyms=USD", timeout=8)
            if resp.status_code == 200 and 'USD' in resp.json():
                price = float(resp.json()['USD'])
                cache_set(crypto_id, price)
                return price
    except Exception as e:
        logger.debug(f"CryptoCompare price fetch failed: {e}")
    
    return None


EXCHANGES = {
    'binance': {
        'url': 'https://api.binance.com/api/v3/ticker/price?symbol={}USDT',
        'parse': lambda j: float(j['price']),
        'name': 'Binance',
    },
    'crypto.com': {
        'url': 'https://api.crypto.com/v2/public/get-ticker?instrument={}_USD',
        'parse': lambda j: float(j['result']['data']['a']),
        'name': 'Crypto.com',
    },
    'kraken': {
        'url': 'https://api.kraken.com/0/public/Ticker?pair={}USD',
        'parse': lambda j: float(list(j['result'].values())[0]['c'][0]),
        'name': 'Kraken',
    },
}

EXCHANGE_NAMES = {e: info['name'] for e, info in EXCHANGES.items()}


def get_exchange_symbol(crypto_id):
    return EXCHANGE_SYMBOL_MAP.get(crypto_id)


def _fetch_exchange_price(symbol, exchange_id):
    info = EXCHANGES.get(exchange_id)
    if not info or not symbol:
        return None
    url = info['url'].format(symbol)
    try:
        resp = session.get(url, timeout=8)
        resp.raise_for_status()
        return info['parse'](resp.json())
    except Exception:
        return None


def get_exchange_price(crypto_id, exchange='coingecko'):
    if exchange == 'coingecko':
        return get_crypto_price(crypto_id)
    symbol = get_exchange_symbol(crypto_id)
    if not symbol:
        return None
    cache_key = f'exch_{exchange}_{crypto_id}'
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    price = _fetch_exchange_price(symbol, exchange)
    if price:
        cache_set(cache_key, price, ttl=60)
    return price


def get_user_exchange(user_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT exchange FROM exchange_prefs WHERE user_id=?", (user_id,))
        row = c.fetchone()
    return row[0] if row else 'coingecko'


def set_user_exchange(user_id, exchange):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO exchange_prefs (user_id, exchange) VALUES (?,?)", (user_id, exchange))
        conn.commit()


# ─────────────────────────────────────────────
# Default display currency (secondary price line)
# Toman by default; switchable to USD / EUR / TRY via /setcurrency.
# ─────────────────────────────────────────────
DISPLAY_CURRENCIES = {
    'toman': {'flag': '💰', 'label': 'Toman'},
    'usd':   {'flag': '💵', 'label': 'USD'},
    'eur':   {'flag': '🇪🇺', 'label': 'EUR'},
    'try':   {'flag': '🇹🇷', 'label': 'TRY'},
}

_currency_cache: dict[int, str] = {}
_currency_cache_lock = threading.Lock()


def db_get_currency(user_id: int) -> str:
    with _currency_cache_lock:
        if user_id in _currency_cache:
            return _currency_cache[user_id]
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT currency FROM currency_prefs WHERE user_id=?", (user_id,))
        row = c.fetchone()
    cur = row[0] if row and row[0] in DISPLAY_CURRENCIES else 'toman'
    with _currency_cache_lock:
        _currency_cache[user_id] = cur
    return cur


def db_set_currency(user_id: int, currency: str) -> None:
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO currency_prefs (user_id, currency) VALUES (?,?)",
            (user_id, currency)
        )
        conn.commit()
    with _currency_cache_lock:
        _currency_cache[user_id] = currency if currency in DISPLAY_CURRENCIES else 'toman'


def _secondary_currency_line(user_id: int, price_usd, usd_to_irr) -> str:
    """The '💰 …' line under a USD price, in the user's chosen display currency.

    Toman → existing Toman line (default).
    TRY/EUR → convert via the toman-per-unit FX rates.
    USD → suppressed (the price itself is already in USD)."""
    if not price_usd:
        return ""
    cur = db_get_currency(user_id)
    if cur == 'toman':
        if not usd_to_irr:
            return ""
        return T(user_id, 'price_toman_line', irr=f"{price_usd * usd_to_irr:,.0f}")
    rate = get_try_to_irr() if cur == 'try' else (get_eur_to_irr() if cur == 'eur' else None)
    if rate is None:
        return ""
    val = usd_to_fiat(price_usd, rate, decimals=2, usd_to_irr=usd_to_irr)
    if val is None:
        return ""
    return T(user_id, f'price_{cur}_line', val=f"{val:,.2f}")


def _portfolio_total_line(user_id: int, total_usd, usd_to_irr) -> str:
    """Portfolio total line, honouring the user's display currency."""
    cur = db_get_currency(user_id)
    if cur in ('eur', 'try') and usd_to_irr:
        rate = get_eur_to_irr() if cur == 'eur' else get_try_to_irr()
        val = usd_to_fiat(total_usd, rate, decimals=2, usd_to_irr=usd_to_irr) if rate else None
        if val is not None:
            return T(user_id, 'portfolio_total_cur',
                     usd=fmt_price(total_usd), val=f"{val:,.2f}",
                     cur=DISPLAY_CURRENCIES[cur]['label'])
    if usd_to_irr:
        return T(user_id, 'portfolio_total', usd=fmt_price(total_usd), irr=f"{total_usd * usd_to_irr:,.0f}")
    return T(user_id, 'portfolio_total', usd=fmt_price(total_usd), irr="N/A")


def _fmt_price_with_exchange(price, exchange):
    if exchange and exchange != 'coingecko':
        name = EXCHANGE_NAMES.get(exchange, exchange.upper())
        return f"{fmt_price(price)} ({name})"
    return fmt_price(price)


def _sparkline(prices, width=15):
    """Generate a text sparkline bar from a list of float prices."""
    if not prices:
        return ""
    mn, mx = min(prices), max(prices)
    rng = mx - mn or 1
    bars = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█']
    return ''.join(bars[min(int((p - mn) / rng * (len(bars) - 1)), len(bars) - 1)] for p in prices)


@rate_limited_api_call
def _fetch_chart_data(crypto_id, days=30):
    """Fetch chart data from CoinGecko as raw list of [timestamp, price] pairs."""
    try:
        resp = session.get(
            f"https://api.coingecko.com/api/v3/coins/{crypto_id}/market_chart",
            params={'vs_currency': 'usd', 'days': days},
            timeout=15
        )
        if resp.status_code == 200:
            return resp.json().get('prices', [])
        elif resp.status_code == 429:
            cache_set('cg_rate_limited', True, ttl=60)
    except Exception as e:
        logger.error(f"Chart data fetch failed for {crypto_id}: {e}")
    return []


def _coincap_interval(days: int) -> str:
    """Map a chart range to a *valid* CoinCap history interval.

    CoinCap only accepts m1/m5/m15/m30/h1/h2/h6/h12/d1 — the previous
    'd7'/'d30' values returned HTTP 400, so the fallback silently died for
    the common 30-day chart (and then reported "no price data from any
    source"). Return h6 for short ranges (finer granularity) and d1 for all
    longer ones (CoinCap keeps ~2000 daily points, plenty for 365 days).
    """
    return 'h6' if days <= 7 else 'd1'


def _fetch_chart_data_fallback(crypto_id, days=30):
    """Fallback: try CoinCap for historical data."""
    coincap_id = _COINCAP_ID_MAP.get(crypto_id, crypto_id)
    if coincap_id == crypto_id and crypto_id == 'the-open-network':
        coincap_id = 'toncoin'
    try:
        resp = session.get(
            f"https://api.coincap.io/v2/assets/{coincap_id}/history",
            params={'interval': _coincap_interval(days)},
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json().get('data', [])
            if not data:
                logger.warning(f"CoinCap returned no chart rows for {crypto_id}")
                return []
            return [[int(d['time']), float(d['priceUsd'])] for d in data]
        else:
            logger.warning(f"CoinCap chart fallback HTTP {resp.status_code} for {crypto_id}")
    except Exception as e:
        logger.error(f"CoinCap chart fallback failed for {crypto_id}: {e}")
    return []


_BINANCE_CHART_SKIP = {'telegram-stars'}


def _fetch_klines_binance(crypto_id, days=30, max_candles=180):
    """Binance klines with real OHLCV for the candlestick chart style.

    Returns [[openTime(ms), open, high, low, close, volume], …] or [] when the
    coin has no exchange symbol / the request fails."""
    if crypto_id in _BINANCE_CHART_SKIP:
        return []
    sym = EXCHANGE_SYMBOL_MAP.get(crypto_id)
    if not sym:
        return []
    # Pick a candle interval that stays within ~max_candles rows
    if days <= 2:
        interval = '1h'
    elif days <= 7:
        interval = '4h'
    elif days <= 30:
        interval = '4h' if days <= 28 else '1d'
    else:
        interval = '1d'
    # Rough candles per day for the given interval
    per_day = {'1h': 24, '4h': 6, '1d': 1}.get(interval, 1)
    limit = min(min(max(days, 1), 365) * per_day, max_candles)
    try:
        resp = session.get(
            "https://api.binance.com/api/v3/klines",
            params={'symbol': f"{sym}USDT", 'interval': interval, 'limit': limit},
            timeout=15
        )
        if resp.status_code != 200:
            logger.warning(f"Binance klines HTTP {resp.status_code} for {crypto_id}")
            return []
        rows = resp.json()
        if not rows:
            logger.warning(f"Binance returned no klines for {crypto_id}")
            return []
        # kline format: [openTime, open, high, low, close, volume, ...]
        return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
                for r in rows if r[1] is not None]
    except Exception as e:
        logger.error(f"Binance klines fetch failed for {crypto_id}: {e}")
    return []


def _fetch_chart_data_binance(crypto_id, days=30):
    """Third fallback: Binance daily klines (OHLCV) as a chart source."""
    if crypto_id in _BINANCE_CHART_SKIP:
        return []
    sym = EXCHANGE_SYMBOL_MAP.get(crypto_id)
    if not sym:
        return []
    try:
        resp = session.get(
            "https://api.binance.com/api/v3/klines",
            params={'symbol': f"{sym}USDT", 'interval': '1d', 'limit': min(max(days, 1), 365)},
            timeout=15
        )
        if resp.status_code != 200:
            logger.warning(f"Binance chart fallback HTTP {resp.status_code} for {crypto_id}")
            return []
        rows = resp.json()
        if not rows:
            logger.warning(f"Binance returned no klines for {crypto_id}")
            return []
        # kline format: [openTime, open, high, low, close, volume, ...]
        return [[int(r[0]), float(r[4])] for r in rows if r[4] is not None]
    except Exception as e:
        logger.error(f"Binance chart fallback failed for {crypto_id}: {e}")
    return []


def get_crypto_chart_image(crypto_id, days=30, user_id=0):
    cache_key = f'chart_{crypto_id}_{days}'
    cached = cache_get(cache_key)
    if cached is not None:
        return cached, crypto_id.upper()
    
    # Validate crypto_id
    if crypto_id not in CRYPTO_LIST and crypto_id != 'telegram-stars':
        raise ValueError(f"Unsupported crypto_id for chart: {crypto_id}")
    
    try:
        raw_prices = _fetch_chart_data(crypto_id, days)
        if not raw_prices:
            logger.warning(f"No CoinGecko chart data for {crypto_id}, trying CoinCap fallback")
            raw_prices = _fetch_chart_data_fallback(crypto_id, days)
        if not raw_prices:
            logger.warning(f"No CoinCap chart data for {crypto_id}, trying Binance fallback")
            raw_prices = _fetch_chart_data_binance(crypto_id, days)
        if not raw_prices:
            raise ValueError("No price data returned from any source")

        timestamps = [p[0] for p in raw_prices]
        prices = [p[1] for p in raw_prices]
        
        # Downsample to ~120 points for the URL API to keep it light
        step = max(1, len(prices) // 120)
        sliced_prices = prices[::step]
        sliced_dates = [datetime.fromtimestamp(ts / 1000, IRAN_TZ).strftime('%b %d') for ts in timestamps[::step]]
        
        # Use Chart.js v4+ compatible config (QuickChart supports this)
        chart_config = {
            "type": "line",
            "data": {
                "labels": sliced_dates,
                "datasets": [{
                    "label": f"{crypto_id.upper()} USD",
                    "data": sliced_prices,
                    "borderColor": "#00cc96",
                    "backgroundColor": "rgba(0, 204, 150, 0.1)",
                    "borderWidth": 2,
                    "fill": True,
                    "pointRadius": 0,
                    "tension": 0.2
                }]
            },
            "options": {
                "plugins": {
                    "legend": {"display": False},
                    "title": {"display": True, "text": f"{crypto_id.upper()} - {days}d", "color": "#ccc", "font": {"size": 16}}
                },
                "scales": {
                    "x": {"grid": {"color": "#333", "borderColor": "#555"}, "ticks": {"color": "#aaa", "maxTicksLimit": 10}},
                    "y": {"grid": {"color": "#333", "borderColor": "#555"}, "ticks": {"color": "#aaa"}}
                },
                "layout": {"padding": 10}
            }
        }
        
        # Try GET first (for shorter URLs)
        config_json = json.dumps(chart_config, separators=(',', ':'))
        qc_url = f"https://quickchart.io/chart?c={url_quote(config_json)}&w=700&h=420&bkg=0e1117&f=png"
        
        # If URL is too long, use POST
        if len(qc_url) > 2000:
            resp = session.post(
                "https://quickchart.io/chart",
                json={
                    "chart": chart_config,
                    "width": 700,
                    "height": 420,
                    "backgroundColor": "0e1117",
                    "format": "png"
                },
                timeout=30
            )
        else:
            resp = session.get(qc_url, timeout=25)
        
        if resp.status_code != 200:
            logger.error(f"QuickChart API failed: {resp.status_code}, response: {resp.text[:500]}")
            # Try with simpler config as last resort
            simple_config = {
                "type": "line",
                "data": {
                    "labels": sliced_dates,
                    "datasets": [{
                        "data": sliced_prices,
                        "borderColor": "#00cc96",
                        "fill": False
                    }]
                },
                "options": {"plugins": {"legend": {"display": False}}, "elements": {"point": {"radius": 0}}}
            }
            simple_json = json.dumps(simple_config, separators=(',', ':'))
            qc_url = f"https://quickchart.io/chart?c={url_quote(simple_json)}&w=700&h=420&bkg=0e1117&f=png"
            if len(qc_url) > 2000:
                resp = session.post(
                    "https://quickchart.io/chart",
                    json={
                        "chart": simple_config,
                        "width": 700,
                        "height": 420,
                        "backgroundColor": "0e1117",
                        "format": "png"
                    },
                    timeout=30
                )
            else:
                resp = session.get(qc_url, timeout=25)
            if resp.status_code != 200:
                raise ValueError(f"QuickChart API failed: {resp.status_code}")
        
        if not resp.content or len(resp.content) < 100:
            raise ValueError("QuickChart returned empty or invalid image")
        
        result = resp.content
        cache_set(cache_key, result, ttl=21600)
        return result, crypto_id.upper()
    except Exception as e:
        logger.error(f"Chart generation failed for {crypto_id}: {e}")
        # Re-raise to let caller handle
        raise


def get_crypto_candle_chart_image(crypto_id, days=30, user_id=0):
    """Candlestick (OHLC) chart style, rendered from live Binance klines.

    Returns (png_bytes, symbol, is_candle); falls back to the line chart when
    the coin has no exchange symbol or the klines request fails."""
    cache_key = f'chart_{crypto_id}_{days}_c'
    cached = cache_get(cache_key)
    if cached is not None:
        return cached, crypto_id.upper(), True

    if crypto_id not in CRYPTO_LIST and crypto_id != 'telegram-stars':
        raise ValueError(f"Unsupported crypto_id for chart: {crypto_id}")

    klines = _fetch_klines_binance(crypto_id, days)
    if not klines:
        logger.info(f"No OHLC data for {crypto_id}, falling back to line chart")
        line = get_crypto_chart_image(crypto_id, days, user_id)
        return line[0], line[1], False

    # Details for the caption: last candle OHLC + volume (same ttl as the chart)
    last = klines[-1]
    cache_set(f'candlemeta_{crypto_id}_{days}',
              {'o': last[1], 'h': last[2], 'l': last[3], 'c': last[4], 'v': last[5]},
              ttl=21600)

    try:
        symbol = EXCHANGE_SYMBOL_MAP.get(crypto_id, crypto_id.upper())
        candles = [{"x": k[0], "o": k[1], "h": k[2], "l": k[3], "c": k[4]} for k in klines]

        chart_config = {
            "type": "candlestick",
            "data": {
                "datasets": [{
                    "label": f"{symbol}/USDT",
                    "data": candles,
                    "backgroundColor": {"up": "#00cc96", "down": "#f6465d", "unchanged": "#8b949e"},
                }]
            },
            "options": {
                "plugins": {
                    "legend": {"display": False},
                    "title": {"display": True, "text": f"{symbol} - {days}d Candles", "color": "#ccc", "font": {"size": 16}},
                },
                "scales": {
                    "x": {"time": {"unit": "day" if days > 7 else "hour", "stepSize": 1, "displayFormats": {"day": "MMM d", "hour": "MMM d HH:mm"}}, "ticks": {"color": "#aaa", "autoSkip": True, "maxTicksLimit": 8}, "grid": {"color": "#333"}},
                    "y": {"ticks": {"color": "#aaa"}, "grid": {"color": "#333"}, "position": "right"}
                },
                "layout": {"padding": 10}
            }
        }

        config_json = json.dumps(chart_config, separators=(',', ':'))
        qc_url = f"https://quickchart.io/chart?v=3&c={url_quote(config_json)}&w=700&h=460&bkg=0e1117&f=png"

        if len(qc_url) > 2000:
            resp = session.post(
                "https://quickchart.io/chart",
                json={
                    "chart": chart_config,
                    "version": "3",
                    "width": 700,
                    "height": 460,
                    "backgroundColor": "0e1117",
                    "format": "png"
                },
                timeout=30
            )
        else:
            resp = session.get(qc_url, timeout=25)

        if resp.status_code != 200 or not resp.content or len(resp.content) < 100:
            logger.warning(f"QuickChart candlestick render failed ({resp.status_code}) for {crypto_id}, falling back to line chart")
            line = get_crypto_chart_image(crypto_id, days, user_id)
            return line[0], line[1], False

        result = resp.content
        cache_set(cache_key, result, ttl=21600)
        return result, symbol, True
    except Exception as e:
        logger.warning(f"Candlestick chart failed for {crypto_id}: {e} — falling back to line chart")
        try:
            line = get_crypto_chart_image(crypto_id, days, user_id)
            return line[0], line[1], False
        except Exception:
            raise


TOP_COINS_FOR_CHART = [
    'bitcoin', 'ethereum', 'tether', 'binancecoin', 'cardano',
    'ripple', 'solana', 'polkadot', 'dogecoin', 'shiba-inu',
    'tron', 'the-open-network',
]


def _prewarm_charts():
    """Pre-generate charts for top coins so the first request is instant."""
    logger.info("Pre-warming chart cache for top coins...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        fut_map = {}
        for cid in TOP_COINS_FOR_CHART:
            cache_key = f'chart_{cid}_30'
            if cache_get(cache_key) is None:
                fut = ex.submit(get_crypto_chart_image, cid)
                fut_map[fut] = cid
        for fut in concurrent.futures.as_completed(fut_map):
            cid = fut_map[fut]
            try:
                fut.result()
                logger.debug(f"Pre-warmed chart for {cid}")
            except Exception as e:
                logger.debug(f"Pre-warm skipped for {cid}: {e}")
    logger.info("Chart pre-warming complete")


def _prewarm_prices():
    """Pre-fetch all crypto prices so inline mode is instant."""
    ids = ','.join(k for k in CRYPTO_LIST if k != 'telegram-stars')
    prices = _fetch_prices_coingecko(ids)
    if prices:
        for cid in CRYPTO_LIST:
            if cid != 'telegram-stars' and cid in prices and 'usd' in prices[cid]:
                cache_set(cid, prices[cid]['usd'])
        logger.info(f"Pre-warmed {len(prices)} price(s)")


def get_portfolio_chart_image(holdings: dict, prices: dict, user_id: int = 0) -> bytes:
    """Pie chart of portfolio allocation by USD value."""
    labels, sizes, colours_pool = [], [], [
        '#00cc96', '#636efa', '#ef553b', '#ab63fa',
        '#ffa15a', '#19d3f3', '#ff6692', '#b6e880',
        '#ff97ff', '#fecb52', '#1f77b4', '#2ca02c'
    ]
    for i, (symbol, amount) in enumerate(holdings.items()):
        crypto_id = detect_currency(symbol.lower())
        price = prices.get(crypto_id) if crypto_id else None
        if price:
            value = amount * price
            if value > 0:
                labels.append(symbol)
                sizes.append(value)

    if not sizes:
        raise ValueError("No priced holdings to chart.")

    wedge_colours = [colours_pool[i % len(colours_pool)] for i in range(len(labels))]
    total = sum(sizes)
    
    # Use Chart.js v3+ compatible config with outlabels plugin
    chart_config = {
        "type": "pie",
        "data": {
            "labels": labels,
            "datasets": [{
                "backgroundColor": wedge_colours,
                "data": sizes,
                "borderWidth": 0
            }]
        },
        "options": {
            "plugins": {
                "legend": {"display": False},
                "title": {"display": True, "text": f"Portfolio Breakdown (Total: ${total:,.2f})", "color": "#fff", "font": {"size": 20}},
                "outlabels": {
                    "text": "%l %p",
                    "color": "white",
                    "stretch": 35,
                    "font": {
                        "resizable": True,
                        "minSize": 12,
                        "maxSize": 18
                    }
                }
            }
        }
    }
    
    qc_url = f"https://quickchart.io/chart?c={url_quote(json.dumps(chart_config, separators=(',', ':')))}&w=700&h=700&bkg=0e1117&f=png"
    resp = session.get(qc_url, timeout=20)
    if resp.status_code != 200:
        logger.error(f"QuickChart API failed for portfolio: {resp.status_code}, response: {resp.text[:500]}")
        raise ValueError(f"QuickChart API failed: {resp.status_code}")
    
    if not resp.content or len(resp.content) < 100:
        raise ValueError("QuickChart returned empty or invalid image")
    
    return resp.content


@rate_limited_api_call(cache_key='usd_to_irr')
def get_usd_to_irr():
    cached = cache_get('usd_to_irr')
    if cached is not None:
        return cached
    try:
        response = session.get(
            'https://apiv2.nobitex.ir/market/stats?srcCurrency=usdt&dstCurrency=rls',
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        if data.get('status') == 'ok' and 'stats' in data:
            usdt_stats = data['stats'].get('usdt-rls', {})
            latest_price = usdt_stats.get('latest')
            if latest_price:
                price = int(float(latest_price) / 10)
                cache_set('usd_to_irr', price, ttl=180)
                logger.info(f"Fetched USD to IRR rate: {price}")
                return price
            best_buy = usdt_stats.get('bestBuy')
            best_sell = usdt_stats.get('bestSell')
            if best_buy and best_sell:
                avg_price = (float(best_buy) + float(best_sell)) / 2
                price = int(avg_price / 10)
                cache_set('usd_to_irr', price, ttl=180)
                return price
    except Exception as e:
        logger.error(f"Error fetching USD/IRR: {e}")

    logger.warning("All USD/IRR sources failed - rate unavailable.")
    # Notify the owner at most once per outage — the 3s API cooldown would
    # otherwise turn this into one DM every ~3 seconds under load.
    if OWNER_USER_ID and not cache_get('usd_irr_outage_notified'):
        cache_set('usd_irr_outage_notified', True, ttl=900)
        try:
            bot.send_message(
                OWNER_USER_ID,
                f"⚠️ <b>USD/IRR API failed</b>\n\nBoth Nobitex sources failed. "
                f"Rate is unavailable.\n\n"
                f"<i>{datetime.now(pytz.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.debug(f"Failed to notify owner about USD/IRR failure: {e}")
    return None


@rate_limited_api_call(cache_key='gold_xau')
def get_gold_prices():
    """Fetch gold prices from CoinGecko (XAU in USD)."""
    cached = cache_get('gold_xau')
    if cached is not None:
        return {'xau': cached}
    
    try:
        # CoinGecko: Gold (XAU) in USD - via rate-limited batch fetcher
        data = _fetch_prices_coingecko('pax-gold')
        if data and 'pax-gold' in data and 'usd' in data['pax-gold']:
            xau_price = data['pax-gold']['usd']
            cache_set('gold_xau', xau_price, ttl=60)
            logger.info(f"Fetched gold price: ${xau_price}")
            return {'xau': xau_price}
    except Exception as e:
        logger.error(f"CoinGecko gold error: {e}")
    
    # Fallback to Binance (PAXG/USDT - gold-backed token)
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
        response = session.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if 'price' in data:
            xau_price = float(data['price'])
            cache_set('gold_xau', xau_price, ttl=60)
            logger.info(f"Fetched gold price from Binance: ${xau_price}")
            return {'xau': xau_price}
    except Exception as e:
        logger.error(f"Binance gold error: {e}")
    
    return {}  # Return empty if all fail


TGJU_CATEGORY_IDS = {
    'grams': 91818,    # geram18, geram24, gold_740k, gold_mini_size
    'mesghal': 91820,  # mesghal, gold_17, gold_17_transfer
    'coins': 28068,    # sekee, sekeb, nim, rob, gerami
}


@rate_limited_api_call(cache_key='iran_gold')
def get_iran_gold_prices():
    """Fetch Iranian gold prices (Toman) from tgju.org."""
    cached = cache_get('iran_gold')
    if cached:
        return cached
    prices = {}
    try:
        for cid in TGJU_CATEGORY_IDS.values():
            r = session.get(
                f'https://api.tgju.org/v1/market/list-data?category_ids={cid}&extra_data=1&lang=fa',
                timeout=10,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            r.raise_for_status()
            data = r.json()
            for row in data.get('data', []):
                m = re.search(r'profile/([a-z0-9_]+)', row[0])
                if not m:
                    continue
                slug = m.group(1)
                price_rial = int(re.sub(r'[^\d]', '', re.sub(r'<[^>]+>', '', row[1])))
                if price_rial:
                    prices[slug] = price_rial // 10  # Rial → Toman
        if prices:
            cache_set('iran_gold', prices, ttl=60)
            logger.info(f"Fetched Iran gold prices from tgju: {list(prices)}")
        return prices
    except Exception as e:
        logger.error(f"tgju gold fetch failed: {e}")
        return {}


@rate_limited_api_call(cache_key=lambda code, cache_key: cache_key)
def _fx_to_irr(code: str, cache_key: str):
    """Fiat currency → Toman rate via exchangerate-api: {code} → USD → IRR."""
    cached = cache_get(cache_key)
    if cached:
        return cached
    try:
        r = session.get(f'https://api.exchangerate-api.com/v4/latest/{code}', timeout=8)
        if r.status_code == 200:
            fx_to_usd = r.json().get('rates', {}).get('USD')
            if fx_to_usd:
                usd_to_irr = get_usd_to_irr()
                if usd_to_irr is None:
                    return None
                rate = int(fx_to_usd * usd_to_irr)
                cache_set(cache_key, rate)
                logger.info(f"{code} rate: {rate} Toman")
                return rate
    except Exception as e:
        logger.error(f"{code} fetch failed: {e}")
    return None


def get_try_to_irr():
    """Turkish Lira → Toman rate."""
    return _fx_to_irr('TRY', 'try_irr')


def get_eur_to_irr():
    """Euro → Toman rate."""
    return _fx_to_irr('EUR', 'eur_irr')


def get_gbp_to_irr():
    """British Pound → Toman rate."""
    return _fx_to_irr('GBP', 'gbp_irr')


def get_aed_to_irr():
    """UAE Dirham → Toman rate."""
    return _fx_to_irr('AED', 'aed_irr')


def get_cny_to_irr():
    """Chinese Yuan → Toman rate."""
    return _fx_to_irr('CNY', 'cny_irr')


def usd_to_fiat(usd_amount, toman_per_unit, decimals: int = 2, usd_to_irr=None):
    """Convert a USD amount into another fiat unit.

    get_*_to_irr() rates are Toman per 1 unit of that currency, so a USD
    amount converts as usd * (toman_per_usd / toman_per_unit).
    Returns None when either rate is unavailable.
    """
    if usd_to_irr is None:
        usd_to_irr = get_usd_to_irr()
    if not usd_to_irr or not toman_per_unit:
        return None
    try:
        return round(float(usd_amount) * float(usd_to_irr) / float(toman_per_unit), decimals)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# ─────────────────────────────────────────────
# Safe math evaluation
# ─────────────────────────────────────────────
def fmt_price(price) -> str:
    """Format a crypto price without scientific notation.
    BTC-sized (≥1): $95,432.12
    Mid-range (≥0.01): $0.28
    Small (<0.01): $0.000412
    """
    if price is None:
        return "-"
    if price >= 1:
        return f"${price:,.2f}"
    elif price >= 0.0001:
        return f"${price:.6f}".rstrip('0').rstrip('.')
    else:
        return f"${price:.8f}".rstrip('0').rstrip('.')


def _normalize_persian(text: str) -> str:
    """
    Convert Persian-Indic digits and operators to ASCII equivalents.
    Uses the enhanced normalize_digits from number_utils.
    """
    return normalize_digits(text)


def _fmt_number(value: str, user_id: int) -> str:
    """Format a number string in the user's locale digits."""
    lang = db_get_lang(user_id)
    return format_for_locale(value, lang)


def evaluate_math(expression, user_id: int = 0):
    try:
        original_expr = expression.strip()
        # Normalize Persian/Arabic-Indic digits and operators to ASCII
        work_expr = _normalize_persian(original_expr).lower()
        # Normalize Persian "از" (az = "of") → "of" for percentage pattern
        work_expr = re.sub(r'\s*از\s*', ' of ', work_expr)

        # Handle "X% of Y"
        if '% of' in work_expr or '%of' in work_expr:
            work_expr = re.sub(
                r'(\d+(?:\.\d+)?)\s*%\s*of\s*(\d+(?:\.\d+)?)',
                r'((\1/100)*\2)', work_expr, flags=re.IGNORECASE
            )
        elif '+' in work_expr and '%' in work_expr:
            match = re.search(r'(\d+(?:\.\d+)?)\s*\+\s*(\d+(?:\.\d+)?)\s*%', work_expr)
            if match:
                base, percent = match.group(1), match.group(2)
                work_expr = f"{base} + ({base}*{percent}/100)"
        elif '-' in work_expr and '%' in work_expr:
            match = re.search(r'(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*%', work_expr)
            if match:
                base, percent = match.group(1), match.group(2)
                work_expr = f"{base} - ({base}*{percent}/100)"

        work_expr = work_expr.replace('%', '').replace(' ', '')
        # Convert ^ to ** for exponentiation
        work_expr = work_expr.replace('^', '**')
        sanitized = re.sub(r'[^\d+\-*/().]', '', work_expr)

        if not sanitized or sanitized == '.':
            return T(user_id, 'invalid_expression')

        # Validate expression contains digits and operators
        if not re.search(r'\d', sanitized) or not re.search(r'[+\-*/]', sanitized):
            return T(user_id, 'invalid_expression')
        
        # Make sure it's not JUST operators/parentheses
        if sanitized.strip() in ['+', '-', '*', '/', '(', ')', '+-', '--', '**', '//']:
            return T(user_id, 'invalid_expression')

        # Guard against CPU DoS: simpleeval computes huge exponent chains
        # (e.g. "9**9**9" -> an astronomically large int) instantly exceeding
        # any digit budget.  Reject chained exponentiation (right-assoc => the
        # top exponent explodes) and any single exponent that is large.
        if sanitized.count('**') > 1:
            return T(user_id, 'invalid_expression')
        for _m in re.finditer(r'\*\*\s*(\d+)', sanitized):
            if len(_m.group(1)) > 6 or int(_m.group(1)) > 1000:
                return T(user_id, 'invalid_expression')

        # Evaluate safely using simpleeval
        try:
            result = simple_eval(sanitized)
        except ZeroDivisionError:
            return T(user_id, 'division_by_zero')
        except Exception:
            return T(user_id, 'invalid_expression')

        # Format result
        if isinstance(result, float) and result == int(result):
            result = int(result)
        expr_display = _normalize_persian(original_expr)
        return T(user_id, 'math_result', expr=expr_display, result=f"{result:,}")
    except Exception as e:
        logger.error(f"Math evaluation error: {e}")
        return T(user_id, 'invalid_expression')


# ─────────────────────────────────────────────
# TRON helpers
# ─────────────────────────────────────────────
def get_tron_wallet_trx(address, user_id: int = 0):
    try:
        # Only plain base58 TRON addresses may reach the API URL
        if not _TRON_ADDR_RE.fullmatch(str(address)):
            return "❌ Invalid TRON address."
        url = f"https://apilist.tronscan.org/api/account?address={address}"
        response = session.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if 'balance' in data:
            # Parse balance with Decimal for precision
            balance_sun = data.get('balance', 0)
            balance_trx = Decimal(str(balance_sun)) / Decimal('1000000')
            
            # Get current TRX price and exchange rate
            trx_price = get_crypto_price('tron')
            usd_to_irr = get_usd_to_irr()
            user_lang = db_get_lang(user_id)
            
            if not trx_price or usd_to_irr is None:
                bal_str = format_crypto(balance_trx)
                bal_str = format_for_locale(bal_str, user_lang)
                return T(user_id, 'wallet_header_tron') + quote(f"🪙 {bal_str} TRX")
            
            balance_display = format_wallet_balance(
                crypto_amount=balance_trx,
                crypto_symbol='TRX',
                usd_rate=Decimal(str(trx_price)),
                toman_rate=Decimal(str(usd_to_irr)),
                user_lang=user_lang
            )
            
            return T(user_id, 'wallet_header_tron') + quote(balance_display)
        return T(user_id, 'no_balance')
    except requests.Timeout:
        logger.error(f"Timeout fetching TRX wallet: {address}")
        return T(user_id, 'tron_timeout')
    except Exception as e:
        logger.error(f"Error fetching TRX wallet: {e}")
        return T(user_id, 'tron_error')


def get_tron_transaction_details(hash_value, user_id: int = 0):
    """Full verbose format for regular messages."""
    return _format_tron_transaction(hash_value, user_id, inline=False)


def get_tron_transaction_details_inline(hash_value, user_id: int = 0):
    """Compact format for inline query results."""
    return _format_tron_transaction(hash_value, user_id, inline=True)


def _format_tron_transaction(hash_value, user_id: int = 0, inline: bool = False):
    try:
        # Transaction hashes must be plain hex before they may appear in a URL
        if not re.fullmatch(r'[0-9a-fA-F]{64}', str(hash_value)):
            return "❌ Invalid transaction hash."
        url = f"https://apilist.tronscan.org/api/transaction-info?hash={hash_value}"
        response = session.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data or 'hash' not in data:
            return T(user_id, 'tx_not_found')
        timestamp = data.get('timestamp', 0)
        time_str = datetime.fromtimestamp(timestamp / 1000, IRAN_TZ).strftime('%Y-%m-%d %H:%M:%S') if timestamp else 'N/A'
        confirmed = data.get('confirmed', False)
        # TRON transaction status: confirmed=true -> success, confirmed=false -> pending
        # Check for failed/rejected status
        ret = data.get('ret', [])
        contract_ret = ret[0].get('contractRet', 'SUCCESS') if ret else 'SUCCESS'
        is_failed = contract_ret != 'SUCCESS'
        
        if is_failed:
            status_emoji = '❌'
            status_text = T(user_id, 'tx_failed') if 'tx_failed' in STRINGS.get('en', {}) else 'Failed'
        elif confirmed:
            status_emoji = '✅'
            status_text = T(user_id, 'tx_confirmed')
        else:
            status_emoji = '⏳'
            status_text = T(user_id, 'tx_pending')
            
        # Block may be missing; use a numeric default so f"{block:,}" never
        # hits ValueError (the old 'N/A' string default crashed the formatter
        # and the transaction fell through to the generic error).
        block = data.get('block') or 0

        hash_escaped = html.escape(str(hash_value))
        
        # ── Shared body: identical for regular messages and inline results ──
        body = T(user_id, 'tx_status', emoji=status_emoji, status=status_text) \
             + T(user_id, 'tx_time', time=time_str) \
             + T(user_id, 'tx_block', block=f"{block:,}")
        if 'contractData' in data:
            contract = data['contractData']
            owner = contract.get('owner_address', 'N/A')
            to = contract.get('to_address', 'N/A')
            amount = contract.get('amount', 0)
            owner_escaped = html.escape(str(owner))
            to_escaped = html.escape(str(to))
            if owner != 'N/A':
                body += T(user_id, 'tx_from', addr=f"<code>{owner_escaped}</code>")
            if to != 'N/A':
                body += T(user_id, 'tx_to', addr=f"<code>{to_escaped}</code>")
            if amount:
                body += T(user_id, 'tx_amount', amount=f"{amount / 1_000_000:,.6f}")
        if 'cost' in data:
            fee = float(data['cost'].get('net_fee', 0)) / 1_000_000
            energy_fee = float(data['cost'].get('energy_fee', 0)) / 1_000_000
            total_fee = fee + energy_fee
            if total_fee > 0:
                body += T(user_id, 'tx_fee', fee=f"{total_fee:,.6f}")
        body += T(user_id, 'tx_hash', hash=f"<code>{hash_escaped}</code>")

        return (
            T(user_id, 'tx_header')
            + quote(body)
            + f"\n🔗 <a href='https://tronscan.org/#/transaction/{hash_escaped}'>View on Tronscan</a>"
        )
    except requests.Timeout:
        return T(user_id, 'tx_timeout')
    except Exception as e:
        logger.error(f"Error fetching transaction: {e}")
        return T(user_id, 'tx_error')


# ─────────────────────────────────────────────
# Currency helpers
# ─────────────────────────────────────────────
def detect_currency(text, check_u_alias=False):
    text_lower = text.lower().strip()
    # Special case: 'u' → 'usdt' only when check_u_alias=True (number present).
    # Accepts the amount-prefixed shorthand too: 'u', '10u', '10 u'.
    if check_u_alias and re.match(r'^(?:\d+(?:\.\d+)?)?\s*u$', text_lower):
        return 'tether'
    if text_lower in CRYPTO_ALIASES:
        return CRYPTO_ALIASES[text_lower]
    if text_lower in FIAT_ALIASES:
        return FIAT_ALIASES[text_lower]
    return None


def convert_amount(amount, src, dst):
    usd_to_irr = get_usd_to_irr()
    if src == dst:
        return amount, None
    if src == "usd":
        amount_usd = amount
    elif src == "toman":
        if usd_to_irr is None:
            return None, "USD/IRR rate unavailable."
        amount_usd = amount / usd_to_irr
    else:
        price_usd = get_crypto_price(src)
        if not price_usd:
            return None, f"Could not fetch price for {src.upper()}."
        amount_usd = amount * price_usd
    if dst == "usd":
        return amount_usd, None
    elif dst == "toman":
        if usd_to_irr is None:
            return None, "USD/IRR rate unavailable."
        return amount_usd * usd_to_irr, None
    else:
        price_usd = get_crypto_price(dst)
        if not price_usd:
            return None, f"Could not fetch price for {dst.upper()}."
        return amount_usd / price_usd, None


# ─────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────
@bot.message_handler(commands=['start', 'help'])
@rate_limit_check
@loading_indicator
def start(message):
    user_id = message.from_user.id
    name = message.from_user.first_name or "there"
    # Force-join check
    if REQUIRED_CHANNEL and _is_joined_channel(user_id) is False:
        _send_join_required(message.chat.id)
        return
    # On first /start, show language picker if no language set yet
    if message.text and message.text.strip().lower() == '/start':
        with get_db_conn() as conn:
            _row = conn.cursor().execute(
                "SELECT lang FROM user_languages WHERE user_id=?", (user_id,)
            ).fetchone()
        if _row is None:
            _send_language_picker(message.chat.id)
            return
    bot.send_message(
        message.chat.id,
        T(user_id, 'start_welcome', name=name,
          bot_username=_bot_username_tag()),
        parse_mode='HTML'
    )
    logger.info(f"User {user_id} started the bot")


def _send_language_picker(chat_id: int):
    """Send the language selection message (used on first /start and /language)."""
    kb = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("🇬🇧 English", callback_data="set_lang_en"),
        types.InlineKeyboardButton("🇮🇷 فارسی",   callback_data="set_lang_fa"),
    ]])
    bot.send_message(
        chat_id,
        "🌐 <b>Choose your language</b>\nزبان خود را انتخاب کنید:",
        parse_mode='HTML',
        reply_markup=kb
    )


@bot.message_handler(commands=['language'])
@rate_limit_check
@loading_indicator
def language_cmd(message):
    _send_language_picker(message.chat.id)


@bot.message_handler(commands=['privacy'])
@rate_limit_check
@loading_indicator
def privacy_cmd(message):
    user_id = message.from_user.id
    bot.reply_to(message, T(user_id, 'privacy_text'), parse_mode='HTML')


@bot.message_handler(commands=['deleteaccount'])
@rate_limit_check
@loading_indicator
def delete_account_cmd(message):
    user_id = message.from_user.id
    kb = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(T(user_id, 'btn_delete_yes'), callback_data="gdpr_delete_confirm"),
        types.InlineKeyboardButton(T(user_id, 'btn_cancel'),     callback_data="gdpr_delete_cancel"),
    ]])
    bot.reply_to(message, T(user_id, 'delete_confirm_prompt'), parse_mode='HTML', reply_markup=kb)


@bot.message_handler(commands=['clearwallets'])
@rate_limit_check
@loading_indicator
def clear_wallets_cmd(message):
    user_id = message.from_user.id
    wallets = db_get_wallets(user_id)
    if not wallets:
        bot.reply_to(message, T(user_id, 'no_wallets'), parse_mode='HTML')
        return
    kb = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(T(user_id, 'btn_clear_wallets'), callback_data="cw_clear_confirm"),
        types.InlineKeyboardButton(T(user_id, 'btn_cancel'),        callback_data="cw_clear_cancel"),
    ]])
    bot.reply_to(message, T(user_id, 'clearwallets_prompt'), parse_mode='HTML', reply_markup=kb)


@bot.message_handler(commands=['setcurrency'])
@rate_limit_check
@loading_indicator
def set_currency_cmd(message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        current = db_get_currency(uid)
        current_name = DISPLAY_CURRENCIES.get(current, {}).get('label', 'Toman')
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(*[types.InlineKeyboardButton(
            f"{info['flag']} {info['label']}", callback_data=f"setcur_{code}"
        ) for code, info in DISPLAY_CURRENCIES.items()])
        bot.reply_to(
            message,
            T(uid, 'cur_current', name=current_name),
            parse_mode='HTML', reply_markup=kb
        )
        return
    cur = args[1].strip().lower()
    if cur not in DISPLAY_CURRENCIES:
        list_str = ', '.join(DISPLAY_CURRENCIES.keys())
        bot.reply_to(
            message,
            T(uid, 'cur_unknown', list=list_str),
            parse_mode='HTML'
        )
        return
    db_set_currency(uid, cur)
    name = DISPLAY_CURRENCIES[cur]['label']
    bot.reply_to(message, T(uid, 'cur_set', name=name), parse_mode='HTML')
    logger.info(f"User {uid} set display currency to {cur}")


@bot.message_handler(commands=['suggest', 'ticket'])
@rate_limit_check
@loading_indicator
def suggest_cmd(message):
    user_id = message.from_user.id
    if not SUGGESTION_CHAT_ID:
        bot.reply_to(message, T(user_id, 'suggest_no_config'))
        return
    set_user_state(user_id, 'awaiting_suggestion')
    bot.reply_to(
        message,
        T(user_id, 'suggest_prompt'),
        parse_mode='HTML'
    )


@bot.message_handler(commands=['donate', 'donation'])
@rate_limit_check
@loading_indicator
def donate_cmd(message):
    user_id = message.from_user.id
    parts = []
    parts.append(T(user_id, 'donate_info'))
    if DONATION_LINK:
        parts.append(f"🌐 <b>Link:</b>\n{DONATION_LINK}")
    if DONATION_WALLETS:
        chains_list = "\n\n".join(
            f"• <b>{chain}</b>\n  <code>{addr}</code>"
            for chain, addr in DONATION_WALLETS.items()
        )
        parts.append(
            f"💰 <b>Donation addresses:</b>\n\n{chains_list}\n\n"
            f"⚠️ Send ONLY on the matching network. "
            f"Sending on a wrong network (e.g. BTC to TRC20) will lose funds."
        )
    if len(parts) < 2:
        bot.reply_to(message, T(user_id, 'donate_no_config'))
        return
    bot.reply_to(message, "\n\n".join(parts), parse_mode='HTML')


def _process_add_wallet(message, user_id, address):
    chat_id = message.chat.id
    chain = detect_wallet_chain(address)
    if chain == 'tron' and not is_valid_tron_address(address):
        chain = None  # looks like TRON but fails checksum
    elif chain == 'ton' and not is_valid_ton_address(address):
        chain = None  # looks like TON but fails validation
    if chain is None:
        bot.send_message(
            chat_id,
            T(user_id, 'wallet_invalid', address=html.escape(address)),
            parse_mode='HTML'
        )
        return
    existing = db_get_wallets(user_id)
    if len(existing) >= MAX_WALLETS_PER_USER:
        bot.send_message(chat_id, T(user_id, 'wallet_limit', max=MAX_WALLETS_PER_USER))
        return
    if db_add_wallet(user_id, address):
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_view_wallets'), callback_data="show_wallets"),
        ]])
        bot.send_message(
            chat_id,
            T(user_id, 'wallet_added', address=html.escape(address), count=len(existing)+1, max=MAX_WALLETS_PER_USER),
            parse_mode='HTML',
            reply_markup=kb
        )
        logger.info(f"User {user_id} added {chain.upper()} wallet {address[:6]}…{address[-4:]}")
    else:
        bot.send_message(chat_id, T(user_id, 'wallet_already_saved'))


# ─────────────────────────────────────────────
# Inline keyboard builders
# ─────────────────────────────────────────────

def _wallet_chain_icon(addr: str) -> str:
    """Return a chain icon prefix for display."""
    chain = detect_wallet_chain(addr)
    if chain == 'ton':
        return '💎'  # TON diamond
    return '🔗'      # TRON default


def build_wallets_keyboard(wallets: list[str], user_id: int = 0) -> types.InlineKeyboardMarkup:
    """One row per wallet: [💎/🔗 Txxx…xxx]  [🗑 Remove]
    Plus an ➕ Add Wallet button at the bottom."""
    keyboard = []
    for i, addr in enumerate(wallets):
        short = f"{addr[:6]}…{addr[-4:]}"
        icon = _wallet_chain_icon(addr)
        keyboard.append([
            types.InlineKeyboardButton(f"{icon} {short}", callback_data=f"wnoop_{i}"),
            types.InlineKeyboardButton(T(user_id, 'btn_remove'), callback_data=f"wrem_{i}"),
        ])
    keyboard.append([
        types.InlineKeyboardButton(T(user_id, 'btn_add_wallet'), callback_data="wadd"),
        types.InlineKeyboardButton(T(user_id, 'btn_close'),      callback_data="wclose"),
    ])
    return types.InlineKeyboardMarkup(keyboard)


def build_holdings_keyboard(holdings: dict, user_id: int = 0) -> types.InlineKeyboardMarkup:
    """One row per coin: [🪙 BTC]  [✏️ Edit]  [💲 Buy Price]  [🗑 Remove]
    Plus ➕ Add, 📊 Chart, and 🗑 Clear All at the bottom.
    When empty: just shows ➕ Add Coin."""
    keyboard = []
    for symbol in holdings:
        keyboard.append([
            types.InlineKeyboardButton(f"🪙 {symbol}", callback_data=f"hnoop_{symbol}"),
            types.InlineKeyboardButton(T(user_id, 'btn_edit'),      callback_data=f"hedit_{symbol}"),
            types.InlineKeyboardButton(T(user_id, 'btn_buy_price'), callback_data=f"hbuy_{symbol}"),
            types.InlineKeyboardButton("🗑", callback_data=f"hrem_{symbol}"),
        ])
    if holdings:
        keyboard.append([
            types.InlineKeyboardButton(T(user_id, 'btn_add_coin'),  callback_data="hadd"),
            types.InlineKeyboardButton(T(user_id, 'btn_chart'),     callback_data="hchart"),
            types.InlineKeyboardButton(T(user_id, 'btn_clear_all'), callback_data="hclearall"),
        ])
    else:
        keyboard.append([types.InlineKeyboardButton(T(user_id, 'btn_add_coin'), callback_data="hadd")])
    return types.InlineKeyboardMarkup(keyboard)


def wallets_message_text(wallets: list[str], user_id: int = 0) -> str:
    if not wallets:
        return T(user_id, 'no_wallets')
    header = T(user_id, 'wallets_header')
    rows = []
    for i, addr in enumerate(wallets, 1):
        chain = detect_wallet_chain(addr)
        chain_tag = '💎 TON' if chain == 'ton' else '🔗 TRON'
        rows.append(f"{i}. {chain_tag}\n   <code>{html.escape(addr)}</code>")
    return header + quote("\n\n".join(rows))


def holdings_message_text(holdings: dict, usd_to_irr, buy_prices: dict = None, user_id: int = 0) -> str:
    if not holdings:
        return T(user_id, 'no_holdings')
    buy_prices = buy_prices or {}
    header = T(user_id, 'portfolio_header')
    rows = []
    total_usd = 0.0
    # Batch-fetch all prices at once to avoid N+1 API calls
    hold_ids = [detect_currency(s.lower()) for s in holdings if detect_currency(s.lower())]
    if hold_ids:
        _fetch_prices_batch(','.join(set(hold_ids)))
    for symbol, amount in holdings.items():
        crypto_id = detect_currency(symbol.lower())
        if crypto_id and crypto_id in CRYPTO_LIST:
            price_usd = get_crypto_price(crypto_id)
            if price_usd:
                value_usd = amount * price_usd
                total_usd += value_usd
                line = f"🪙 <b>{symbol}</b>  {amount:,.6g} · <i>{fmt_price(value_usd)}</i>"
                buy = buy_prices.get(symbol.upper())
                if buy and buy > 0:
                    pnl_usd = (price_usd - buy) * amount
                    pnl_pct = ((price_usd - buy) / buy) * 100
                    arrow = "📈" if pnl_usd >= 0 else "📉"
                    sign = "+" if pnl_usd >= 0 else ""
                    line += f"\n   {arrow} {sign}${pnl_usd:,.2f} ({sign}{pnl_pct:.1f}%)  {T(user_id, 'buy_at', price=fmt_price(buy))}"
                rows.append(line)
            else:
                rows.append(f"🪙 <b>{symbol}</b>  {amount:,.6g} · {T(user_id, 'price_unavail_short')}")
        else:
            rows.append(f"🪙 <b>{symbol}</b>  {amount:,.6g}")
    body = quote("\n".join(rows)) if rows else ""
    total = _portfolio_total_line(user_id, total_usd, usd_to_irr)
    return header + body + total


# ─────────────────────────────────────────────
# Callback query handler (button taps)
# ─────────────────────────────────────────────
_answered_callbacks: dict[str, float] = {}
_answered_callbacks_lock = threading.Lock()
CALLBACK_ANSWER_TTL = 300.0


def _answer_call(call, text: str | None = None, show_alert: bool = False, **kwargs):
    """Answer a callback query at most once.

    Telegram only accepts a single answerCallbackQuery per callback id, so a
    second answer raises apihelper.QueryAnswerInvalid / QueryAnswerTooLate.
    Several handlers used to answer twice on one code path; this guard keeps the
    first answer and silently ignores the rest.
    """
    call_id = getattr(call, 'id', None)
    if not call_id:
        return
    now = time.time()
    with _answered_callbacks_lock:
        if call_id in _answered_callbacks:
            return
        if len(_answered_callbacks) > 512:
            cutoff = now - CALLBACK_ANSWER_TTL
            for cid, ts in list(_answered_callbacks.items()):
                if ts < cutoff:
                    del _answered_callbacks[cid]
        _answered_callbacks[call_id] = now
    try:
        bot.answer_callback_query(call_id, text=text, show_alert=show_alert, **kwargs)
    except Exception:
        logger.debug(f"answerCallbackQuery failed for {call_id}")


def _edit_call(call, text, reply_markup=None):
    """Edit the message a callback came from (HTML mode).

    Replaces the ~20 copy-pasted try/edit/except blocks: failures are logged
    and ignored (the chat already shows either the old message or the
    fallback toast sent by the caller).
    """
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.debug(f"Failed to edit message: {e}")


def _chart_render(call, user_id: int, crypto: str, days: int, days_label: str, candles: bool = False):
    """Shared chart renderer for the range/style buttons.

    Builds the photo caption (with OHLC details for the candlestick style),
    swaps media in place when possible, and attaches range + style buttons."""
    _answer_call(call, T(user_id, 'generating_chart'))
    # Show loading message that will be replaced
    loading_msg = None
    try:
        loading_msg = bot.send_message(
            call.message.chat.id,
            add_timestamp(T(user_id, 'loading_chart')),
            parse_mode='HTML'
        )
    except Exception:
        pass
    try:
        is_candle = candles
        if candles:
            img_bytes, symbol, is_candle = get_crypto_candle_chart_image(crypto, days, user_id)
        else:
            img_bytes, symbol = get_crypto_chart_image(crypto, days, user_id)
        price = get_crypto_price(crypto)
        kb = types.InlineKeyboardMarkup(row_width=4)
        cb_prefix = "chc_" if candles else "chart_"
        kb.add(*[types.InlineKeyboardButton(
            (f"✓ {d}" if d == days_label else d), callback_data=f"{cb_prefix}{crypto}_{d}"
        ) for d in CHART_DAYS])
        # Style toggle row: current style is marked ✓
        line_label = T(user_id, 'btn_style_line')
        candle_label = T(user_id, 'btn_style_candles')
        if candles:
            kb.add(
                types.InlineKeyboardButton(line_label, callback_data=f"chart_{crypto}_{days_label}"),
                types.InlineKeyboardButton(f"✓ {candle_label}", callback_data=f"chc_{crypto}_{days_label}"),
            )
        else:
            kb.add(
                types.InlineKeyboardButton(f"✓ {line_label}", callback_data=f"chart_{crypto}_{days_label}"),
                types.InlineKeyboardButton(candle_label, callback_data=f"chc_{crypto}_{days_label}"),
            )
        # Header + quoted live price (+ OHLC details in candle style)
        caption = (f"🕯 <b>{symbol}</b> · {days}d"
                   if is_candle else f"📊 <b>{symbol}</b> · {days}d")
        if price:
            caption += "\n" + quote(f"💵 <b>{fmt_price(price)}</b>")
        if is_candle:
            meta = cache_get(f'candlemeta_{crypto}_{days}')
            if meta:
                caption += "\n" + quote(T(
                    user_id, 'chart_ohlc',
                    o=fmt_price(meta.get('o')), h=fmt_price(meta.get('h')),
                    l=fmt_price(meta.get('l')), c=fmt_price(meta.get('c')),
                    v=f"{meta.get('v', 0):,.0f}", sym=symbol
                ))
        if call.message.photo:
            bot.edit_message_media(
                types.InputMediaPhoto(BytesIO(img_bytes), caption=add_timestamp(caption), parse_mode='HTML'),
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=kb
            )
            if loading_msg:
                try:
                    bot.delete_message(loading_msg.chat.id, loading_msg.message_id)
                except Exception:
                    pass
            return  # media edited in place — do NOT send a duplicate photo
        else:
            # Not a photo message - replace entirely
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
        if loading_msg:
            try:
                bot.delete_message(loading_msg.chat.id, loading_msg.message_id)
            except Exception:
                pass
        bot.send_photo(
            call.message.chat.id, photo=BytesIO(img_bytes),
            caption=add_timestamp(caption), parse_mode='HTML', reply_markup=kb
        )
    except Exception as e:
        logger.error(f"Chart callback failed: {e}")
        if loading_msg:
            try:
                bot.edit_message_text(
                    add_timestamp(T(user_id, 'chart_fail')),
                    chat_id=call.message.chat.id, message_id=loading_msg.message_id,
                    parse_mode='HTML'
                )
            except Exception:
                pass
        else:
            _answer_call(call, T(user_id, 'chart_fail'), show_alert=True)


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if call.message is None:
        try:
            _answer_call(call)
        except Exception as e:
            logger.debug(f"Failed to answer callback query: {e}")
        return
    user_id = call.from_user.id
    data = call.data
    message_id = call.message.message_id

    # ── Force-join verification ────────────────────────────────────────
    if data == "check_join":
        if not REQUIRED_CHANNEL:
            _answer_call(call, "No channel configured.")
            return
        with _joined_cache_lock:
            _joined_cache.pop(user_id, None)
        joined = _is_joined_channel(user_id)
        if joined is True:
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception as e:
                logger.debug(f"Failed to delete message: {e}")
            _answer_call(call, "✅ Welcome! You're verified.")
            # Re-send welcome
            name = call.from_user.first_name or "there"
            with get_db_conn() as conn:
                _row = conn.cursor().execute(
                    "SELECT lang FROM user_languages WHERE user_id=?", (user_id,)
                ).fetchone()
            if _row is None:
                _send_language_picker(call.message.chat.id)
            else:
                bot.send_message(
                    call.message.chat.id,
                    T(user_id, 'start_welcome', name=name,
                      bot_username=_bot_username_tag()),
                    parse_mode='HTML'
                )
        else:
            _answer_call(call, "❌ Not yet joined. Please join the channel first.", show_alert=True)
        return

    # ⭐ ADMIN ACTIONS - Must be BEFORE security check
    if data == "admin_broadcast":
        if not is_owner(user_id):
            _answer_call(call, "⛔ Owner only", show_alert=True)
            return
        
        set_user_state(user_id, 'admin_broadcast')
        _answer_call(call)
        _edit_call(call, 
        "📢 <b>Broadcast Message</b>\n\n"
                "Send the message you want to broadcast to all users.\n\n"
                "<i>This will be sent to ALL users who have used the bot.</i>\n\n"
                "To cancel, send /cancel"
        )
        return
    
    if data == "admin_clear_cache":
        if not is_owner(user_id):
            _answer_call(call, "⛔ Owner only", show_alert=True)
            return
        
        with _cache_lock:
            _cache.clear()
        _answer_call(call, "✅ Cache cleared!", show_alert=True)
        logger.info(f"Cache cleared by owner {user_id}")
        return
    
    if data == "admin_stats":
        if not is_owner(user_id):
            _answer_call(call, "⛔ Owner only", show_alert=True)
            return
        
        # Get detailed stats
        with get_db_conn() as conn:
            c = conn.cursor()
            
            # Users by language
            c.execute("SELECT lang, COUNT(*) FROM user_languages GROUP BY lang")
            lang_stats = c.fetchall()
            
            # Top crypto alerts
            c.execute("""
                SELECT crypto_id, COUNT(*) as cnt 
                FROM alerts 
                GROUP BY crypto_id 
                ORDER BY cnt DESC 
                LIMIT 5
            """)
            top_alerts = c.fetchall()
            
        
        msg = "📊 <b>Detailed Statistics</b>\n"
        lang_rows = [
            f"🌐 {'English' if lang == 'en' else 'Persian'}: <b>{count}</b>"
            for lang, count in lang_stats
        ]
        msg += quote("\n".join(lang_rows)) if lang_rows else ""
        alert_rows = []
        for crypto_id, count in top_alerts:
            name = CRYPTO_LIST.get(crypto_id, crypto_id)
            if '(' in name:
                sym = _sym(crypto_id)
            else:
                sym = crypto_id.upper()
            alert_rows.append(f"🔔 <b>{sym}</b>: {count}")
        if alert_rows:
            msg += "\n<b>Top Alert Coins:</b>\n" + quote("\n".join(alert_rows))
        
        _answer_call(call)
        _edit_call(call, 
        add_timestamp(msg)
        )
        return
    
    # ⭐ SECURITY: Check panel ownership (except for language selection)
    if not data.startswith("set_lang_"):
        cleanup_expired_panels()
        
        if not check_panel_owner(call.message.chat.id, message_id, user_id):
            _answer_call(
                call,
                "⚠️ This panel belongs to another user.\n"
                "این پنل متعلق به کاربر دیگری است.",
                show_alert=True
            )
            return

    # ── Language selection ────────────────────────────────────────────
    if data in ("set_lang_en", "set_lang_fa"):
        lang = data.split("_")[2]  # 'en' or 'fa'
        db_set_lang(user_id, lang)
        _answer_call(call)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        # Send confirmation in the NEW language
        toast = T(user_id, 'lang_set_en') if lang == 'en' else T(user_id, 'lang_set_fa')
        bot.send_message(call.message.chat.id, toast, parse_mode='HTML')
        # Force-join check before showing welcome
        if REQUIRED_CHANNEL and _is_joined_channel(user_id) is False:
            _send_join_required(call.message.chat.id)
            return
        # Then immediately show /start welcome
        name = call.from_user.first_name or "there"
        bot.send_message(
            call.message.chat.id,
            T(user_id, 'start_welcome', name=name,
              bot_username=_bot_username_tag()),
            parse_mode='HTML'
        )
        logger.info(f"User {user_id} set language to {lang}")
        return

    # ── Convert wizard ────────────────────────────────────────────────
    if data.startswith("cvt1_"):
        from_cid = data[5:]
        from_sym = _sym(from_cid)
        # Second-leg picker: crypto coins + usd + toman, minus the chosen coin
        picker_coins = [c for c in list(CRYPTO_LIST.keys()) + ['usd', 'toman']
                        if c != from_cid]
        _answer_call(call)
        kb = _coin_picker_keyboard(f"cvt2_{from_cid}_", coins=picker_coins)
        _edit_call(call, T(user_id, 'convert_step2', sym=from_sym), reply_markup=kb)
        return

    if data.startswith("cvt2_"):
        _, from_cid, to_cid = data.split("_", 2)
        from_sym = _sym(from_cid)
        to_sym   = _sym(to_cid)
        set_user_state(user_id, f"convert_{from_cid}_{to_cid}")
        _answer_call(call)
        _edit_call(call, 
        T(user_id, 'convert_step3', from_sym=from_sym, to_sym=to_sym), reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, "btn_cvt_cancel"), callback_data="cvt_cancel")
                ]])
        )
        return

    if data == "cvt_cancel":
        _answer_call(call)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        del_user_state(user_id)
        return

    # ── Stars amount calculator ───────────────────────────────────
    if data == "stars_calc":
        set_user_state(user_id, 'stars_amount')
        _answer_call(call)
        _edit_call(call, 
        T(user_id, 'stars_prompt'), reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, "btn_cvt_cancel"), callback_data="stars_cancel")
                ]])
        )
        return

    if data == "stars_cancel":
        _answer_call(call)
        del_user_state(user_id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if data == "digest_cancel":
        _answer_call(call)
        del_user_state(user_id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    # ── Compare coin picker (step 1 & 2) ─────────────────────────────
    if data.startswith("cmp1_"):
        cid1 = data[5:]
        # Second-leg picker: exclude the already-chosen coin
        sym1 = _sym(cid1)
        _answer_call(call)
        kb = _coin_picker_keyboard(f"cmp2_{cid1}_",
                                   coins=[cid for cid in CRYPTO_LIST if cid != cid1])
        _edit_call(call, T(user_id, 'compare_pick2', sym=sym1), reply_markup=kb)
        return

    if data.startswith("cmp2_"):
        _, cid1, cid2 = data.split("_", 2)
        if cid1 not in CRYPTO_LIST or cid2 not in CRYPTO_LIST:
            _answer_call(call, T(user_id, 'unknown_coin_short'))
            return
        _answer_call(call, T(user_id, 'fetching'))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        _do_compare(call.message, cid1, cid2, user_id)
        return

    if data.startswith("cmpref_"):
        _, cid1, cid2 = data.split("_", 2)
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            _answer_call(call, msg, show_alert=True)
            return
        _answer_call(call, T(user_id, "refreshing"))
        _do_compare(call.message, cid1, cid2, user_id, edit_msg_id=call.message.message_id)
        return

    # ── Alert wizard (step 1: coin → step 2: direction → step 3: price) ──
    if data == "alrt_cancel":
        _answer_call(call)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if data == "alrt_new":
        _answer_call(call)
        kb = _coin_picker_keyboard("alrt1_", cancel="alrt_cancel", user_id=user_id)
        try:
            bot.send_message(
                call.message.chat.id,
                T(user_id, 'alert_step1'),
                parse_mode='HTML',
                reply_markup=kb
            )
        except Exception:
            pass
        return

    if data.startswith("alrt1_"):
        cid = data[6:]
        if cid not in CRYPTO_LIST:
            _answer_call(call, T(user_id, 'unknown_coin_short'))
            return
        sym = _sym(cid)
        price = get_crypto_price(cid)
        price_str = fmt_price(price) if price else "-"
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_above'), callback_data=f"alrt2_{cid}_above"),
            types.InlineKeyboardButton(T(user_id, 'btn_below'), callback_data=f"alrt2_{cid}_below"),
        ],[
            types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="alrt_cancel"),
        ]])
        _answer_call(call)
        _edit_call(call, 
        T(user_id, 'alert_step2', sym=sym, price=price_str), reply_markup=kb
        )
        return

    if data.startswith("alrt2_"):
        parts = data.split("_")
        if len(parts) < 3:
            _answer_call(call, T(user_id, 'invalid_data'))
            return
        cid, direction = parts[1], parts[2]
        # Callback data is user-forgeable: validate both fields before the
        # coin id reaches get_crypto_price / the CoinGecko query string.
        if cid not in CRYPTO_LIST or direction not in ("above", "below"):
            _answer_call(call, T(user_id, 'invalid_data'))
            return
        sym = _sym(cid)
        price = get_crypto_price(cid)
        price_str = fmt_price(price) if price else "-"
        arrow = "📈" if direction == "above" else "📉"
        set_user_state(user_id, f"alert_price_{cid}_{direction}")
        _answer_call(call)
        _edit_call(call, 
        T(user_id, 'alert_step3', sym=sym, price=price_str, arrow=arrow, direction=direction), reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="alrt_cancel")
                ]])
        )
        return

    if data == "show_alerts":
        _answer_call(call)
        alerts = db_get_alerts(user_id)
        if not alerts:
            bot.send_message(
                call.message.chat.id,
                T(user_id, 'no_alerts_simple'),
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, 'btn_set_alert'), callback_data="alrt_new")
                ]])
            )
            return
        keyboard = []
        above_w = T(user_id, 'above_word')
        below_w = T(user_id, 'below_word')
        header = T(user_id, 'alerts_header', count=len(alerts), max=MAX_ALERTS_PER_USER)
        body = []
        # Batch-fetch all alert prices at once
        alert_ids = [a['crypto_id'] for a in alerts if a['crypto_id'] in CRYPTO_LIST]
        if alert_ids:
            _fetch_prices_batch(','.join(set(alert_ids)))
        for a in alerts:
            arrow = '📈' if a['direction'] == 'above' else '📉'
            cur   = get_crypto_price(a['crypto_id'])
            dword = above_w if a['direction'] == 'above' else below_w
            if cur:
                pct_str = f"{abs((a['target_price']-cur)/cur)*100:.1f}"
                dist = f"  <i>{T(user_id, 'away_pct', pct=pct_str)}</i>"
            else:
                dist = ""
            body.append(f"{arrow} <b>{a['symbol']}</b> {dword} <b>{fmt_price(a['target_price'])}</b>{dist}")
            keyboard.append([types.InlineKeyboardButton(
                f"🗑  {a['symbol']} {dword} {fmt_price(a['target_price'])}",
                callback_data=f"alertdel_{a['id']}"
            )])
        text = header + (quote("\n".join(body)) if body else "")
        keyboard.append([
            types.InlineKeyboardButton(T(user_id, 'btn_add_alert'),  callback_data="alrt_new"),
            types.InlineKeyboardButton(T(user_id, 'btn_delete_all'), callback_data="alertdelall"),
        ])
        bot.send_message(
            call.message.chat.id,
            text,
            parse_mode='HTML',
            reply_markup=types.InlineKeyboardMarkup(keyboard)
        )
        return

    # Route alert + digest callbacks to their handler
    if data.startswith("alertdel") or data.startswith("digest_"):
        _handle_alert_callbacks(call, data, user_id)
        return

    # ── Wallet callbacks ──────────────────────
    if data == "wnoop_0" or data.startswith("wnoop_"):
        # tapping the address label does nothing
        _answer_call(call)
        return

    if data.startswith("wrem_"):
        parts = data.split("_")
        idx_str = parts[1] if len(parts) > 1 else ''
        # String isdigit guard — negative indices must not be accepted:
        # "wrem_-1" used to delete the LAST wallet.
        if not idx_str.isdigit():
            _answer_call(call, T(user_id, 'invalid_data'))
            return
        idx = int(idx_str)
        wallets = db_get_wallets(user_id)
        if idx >= len(wallets):
            _answer_call(call, T(user_id, 'wallet_not_found'))
            return
        address = wallets[idx]
        db_remove_wallet(user_id, address)
        logger.info(f"User {user_id} removed wallet {address[:6]}…{address[-4:]}")
        wallets = db_get_wallets(user_id)
        _edit_call(call, 
        wallets_message_text(wallets, user_id), reply_markup=build_wallets_keyboard(wallets, user_id)
        )
        _answer_call(call, T(user_id, 'wallet_removed_toast'))
        return

    if data == "wadd":
        _answer_call(call)
        set_user_state(user_id, 'add_wallet_inline')
        bot.send_message(call.message.chat.id, T(user_id, 'send_wallet_addr'))
        return

    if data == "hclose" or data == "wclose":
        _answer_call(call)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            logger.debug(f"Failed to delete message: {e}")
        return

    if data == "market_refresh":
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            _answer_call(call, msg, show_alert=True)
            return
        _answer_call(call, T(user_id, 'refreshing'))
        market_cmd(call.message, user_id=user_id, edit_msg_id=call.message.message_id)
        return

    # Price list refresh
    if data == "refresh_all_prices":
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            _answer_call(call, msg, show_alert=True)
            return
        _answer_call(call, T(user_id, 'refreshing'))
        ids = ','.join(CRYPTO_LIST.keys())
        prices = _fetch_prices_batch(ids)
        text = _build_price_list_message(user_id, prices)
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_refresh'), callback_data="refresh_all_prices")
        ]])
        _edit_call(call, 
        add_timestamp(text), reply_markup=kb
        )
        return

    # Price refresh button on chart messages
    if data.startswith("refresh_"):
        crypto = data[len("refresh_"):]
        # Callback data is user-forgeable: only allow real coin ids here,
        # otherwise an unknown id flows straight into the CoinGecko query
        # string and pollutes the cache (alrt1_/hpick_/chart_ already do this).
        if crypto not in CRYPTO_LIST:
            _answer_call(call, T(user_id, 'unknown_coin', sym=crypto))
            return
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            _answer_call(call, msg, show_alert=True)
            return
        _answer_call(call, T(user_id, 'refreshing'))
        # Invalidate cache for this coin
        with _cache_lock:
            _cache.pop(crypto, None)
        price_usd = get_crypto_price(crypto)
        usd_to_irr = get_usd_to_irr()
        if not price_usd or usd_to_irr is None:
            _answer_call(call, T(user_id, 'price_fetch_fail'), show_alert=True)
            return
        crypto_name = CRYPTO_LIST.get(crypto, crypto.upper())
        refresh_kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_refresh'), callback_data=f"refresh_{crypto}")
        ]])
        new_caption = (f"📊 {crypto_name}\n"
                       + quote(f"💵 <b>{fmt_price(price_usd)}</b>\n"
                               + _secondary_currency_line(user_id, price_usd, usd_to_irr)))
        new_caption = add_timestamp(new_caption)
        try:
            bot.edit_message_caption(
                caption=new_caption,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=refresh_kb
            )
        except Exception:
            pass
        return

    if data == "gdpr_delete_confirm":
        _answer_call(call)
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM holdings    WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM buy_prices  WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM wallets     WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM alerts      WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM digest_prefs WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM exchange_prefs WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM currency_prefs WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM user_languages WHERE user_id=?", (user_id,))
            conn.commit()
        del_user_state(user_id)
        with _lang_cache_lock:
            _lang_cache.pop(user_id, None)
        logger.info(f"GDPR delete: all data removed for user {user_id}")
        _edit_call(call, 
        T(user_id, 'delete_done')
        )
        return

    if data == "gdpr_delete_cancel":
        _answer_call(call, T(user_id, 'gdpr_cancelled'))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    # ── Clear-wallets confirmation ─────────────────────────────
    if data == "cw_clear_confirm":
        _answer_call(call)
        removed = db_clear_wallets(user_id)
        logger.info(f"User {user_id} cleared {removed} saved wallets")
        _edit_call(call, T(user_id, 'clearwallets_done'))
        return

    if data == "cw_clear_cancel":
        _answer_call(call, T(user_id, 'cancelled'))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    # ── Display-currency picker ────────────────────────────────
    if data.startswith("setcur_"):
        cur = data[len("setcur_"):]
        if cur not in DISPLAY_CURRENCIES:
            _answer_call(call, T(user_id, 'invalid_data'))
            return
        db_set_currency(user_id, cur)
        name = DISPLAY_CURRENCIES[cur]['label']
        _answer_call(call, T(user_id, 'cur_set', name=name))
        _edit_call(call, T(user_id, 'cur_set', name=name))
        return

    if data == "show_holdings":
        _answer_call(call)
        saved = db_get_holdings(user_id) or {}
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        bot.send_message(
            call.message.chat.id,
            holdings_message_text(saved, usd_to_irr, buy_prices, user_id),
            parse_mode='HTML',
            reply_markup=build_holdings_keyboard(saved, user_id)
        )
        return

    if data == "show_wallets":
        _answer_call(call)
        wallets = db_get_wallets(user_id)
        bot.send_message(
            call.message.chat.id,
            wallets_message_text(wallets, user_id),
            parse_mode='HTML',
            reply_markup=build_wallets_keyboard(wallets, user_id)
        )
        return

    if data.startswith("hnoop_"):
        _answer_call(call)
        return

    if data.startswith("hrem_"):
        symbol = data[len("hrem_"):]
        _answer_call(call)
        db_remove_holding(user_id, symbol)
        db_delete_buy_price(user_id, symbol)
        logger.info(f"User {user_id} removed holding {symbol}")
        holdings = db_get_holdings(user_id) or {}
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        _edit_call(call, 
        holdings_message_text(holdings, usd_to_irr, buy_prices), reply_markup=build_holdings_keyboard(holdings)
        )
        _answer_call(call, T(user_id, "holding_removed_toast", sym=symbol))
        return

    if data.startswith("hedit_"):
        symbol = data[len("hedit_"):]
        _answer_call(call)
        set_user_state(user_id, f'edit_holding_{symbol}')
        bot.send_message(call.message.chat.id, T(user_id, 'edit_amount_prompt', sym=symbol), parse_mode='HTML')
        return

    if data.startswith("hbuy_"):
        symbol = data[len("hbuy_"):]
        _answer_call(call)
        set_user_state(user_id, f'set_buy_price_{symbol}')
        bot.send_message(call.message.chat.id, T(user_id, 'buy_price_prompt', sym=symbol), parse_mode='HTML')
        return

    if data == "hchart":
        _answer_call(call, T(user_id, 'generating_chart'))
        holdings = db_get_holdings(user_id) or {}
        if not holdings:
            bot.send_message(call.message.chat.id, T(user_id, 'no_holdings_chart'))
            return
        # Fetch all prices
        prices = {}
        for symbol in holdings:
            cid = detect_currency(symbol.lower())
            if cid:
                p = get_crypto_price(cid)
                if p:
                    prices[cid] = p
        user_id_cb = call.from_user.id
        loading_msg = None
        try:
            loading_msg = bot.send_message(
                call.message.chat.id,
                add_timestamp(T(user_id_cb, 'loading_portfolio')),
                parse_mode='HTML'
            )
        except Exception:
            pass
        try:
            img = get_portfolio_chart_image(holdings, prices, user_id)
            if loading_msg:
                try:
                    bot.delete_message(loading_msg.chat.id, loading_msg.message_id)
                except Exception:
                    pass
            # Apply blockquote formatting to chart caption
            bot.send_photo(
                call.message.chat.id,
                photo=BytesIO(img),
                caption=add_timestamp(quote(T(user_id, 'chart_caption'))),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Portfolio chart failed: {e}")
            if loading_msg:
                try:
                    bot.delete_message(loading_msg.chat.id, loading_msg.message_id)
                except Exception:
                    pass
            bot.send_message(call.message.chat.id, add_timestamp(T(user_id, 'chart_fail')))

    if data == "hpick_cancel":
        _answer_call(call)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        del_user_state(user_id)
        return

    if data.startswith("hpick_"):
        cid = data[6:]
        if cid not in CRYPTO_LIST:
            _answer_call(call, T(user_id, 'unknown_coin_short'))
            return
        sym   = _sym(cid)
        price = get_crypto_price(cid)
        price_str = T(user_id, 'now_price', price=fmt_price(price)) if price else ""
        set_user_state(user_id, f"hpick_amount_{cid}")
        _answer_call(call)
        _edit_call(call, 
        T(user_id, 'coin_amount_prompt', sym=sym, price=price_str), reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="hpick_cancel")
                ]])
        )
        return

    if data == "hadd":
        _answer_call(call)
        _show_holding_coin_picker(
            call.message.chat.id,
            T(user_id, 'add_coin_prompt'),
            user_id
        )
        return

    if data == "hclearall":
        _answer_call(call)
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_yes_clear'), callback_data="hclearall_confirm"),
            types.InlineKeyboardButton(T(user_id, 'btn_cancel'),    callback_data="hclearall_cancel"),
        ]])
        bot.send_message(
            call.message.chat.id,
            T(user_id, 'clear_all_prompt'),
            parse_mode='HTML',
            reply_markup=kb
        )
        return

    if data == "hclearall_confirm":
        _answer_call(call)
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
            # Also clear buy prices — otherwise P&L rows are orphaned
            c.execute("DELETE FROM buy_prices WHERE user_id=?", (user_id,))
            conn.commit()
        _edit_call(call, 
        T(user_id, 'holdings_cleared')
        )
        return

    if data == "hclearall_cancel":
        _answer_call(call, T(user_id, 'cancelled'))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    # ── Chart coin picker (step 1) → time range buttons ─────
    if data.startswith("chr1_"):
        cid = data[5:]
        if cid not in CRYPTO_LIST:
            _answer_call(call, T(user_id, 'unknown_coin_short'))
            return
        sym = _sym(cid)
        kb = types.InlineKeyboardMarkup(row_width=4)
        kb.add(*[types.InlineKeyboardButton(d, callback_data=f"chart_{cid}_{d}") for d in CHART_DAYS])
        _answer_call(call)
        _edit_call(call, 
        T(user_id, 'chart_pick_days', sym=sym), reply_markup=kb
        )
        return

    # ── Chart range selector ──────────────────────────────────
    if data.startswith("chart_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            _, crypto, days_label = parts
            if days_label not in CHART_DAYS:
                # Forgeable callback data — never echo it back into HTML
                _answer_call(call, T(user_id, 'invalid_data'))
                return
            days = CHART_DAYS.get(days_label, 30)
            _chart_render(call, user_id, crypto, days, days_label, candles=False)
        return

    # ── Chart range selector (candlestick style) ──────────────
    if data.startswith("chc_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            _, crypto, days_label = parts
            if days_label not in CHART_DAYS:
                _answer_call(call, T(user_id, 'invalid_data'))
                return
            days = CHART_DAYS.get(days_label, 30)
            _chart_render(call, user_id, crypto, days, days_label, candles=True)
        return

    # ── Exchange selector ─────────────────────────────────────
    if data.startswith("setex_"):
        exch = data.split("_", 1)[1]
        if exch not in EXCHANGES and exch != 'coingecko':
            # Always clear the loading spinner, even on unknown values
            _answer_call(call, T(user_id, 'invalid_data'))
            return
        set_user_exchange(user_id, exch)
        name = EXCHANGE_NAMES.get(exch, 'CoinGecko')
        _answer_call(call, T(user_id, 'exch_set', name=name))
        _edit_call(call, T(user_id, 'exch_set', name=name))
        return

    _answer_call(call)


# Shared executor for wallet balance lookups (bounded, lazy thread spawn).
_WALLET_BALANCE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=5, thread_name_prefix='wallet-bal')


@bot.message_handler(commands=['wallets'])
@rate_limit_check
@loading_indicator
def show_wallets_only(message):
    user_id = message.from_user.id
    wallets = db_get_wallets(user_id)
    bot.reply_to(
        message,
        wallets_message_text(wallets, user_id),
        parse_mode='HTML',
        reply_markup=build_wallets_keyboard(wallets, user_id)
    )


@bot.message_handler(commands=['mywallets'])
@rate_limit_check
@loading_indicator
def show_wallets_with_balance(message):
    user_id = message.from_user.id
    wallets = db_get_wallets(user_id)
    if not wallets:
        bot.reply_to(message, T(user_id, 'no_wallets_yet'))
        return
    bot.send_chat_action(message.chat.id, 'typing')
    # Shared, bounded pool: reusing one executor avoids the thread churn that a
    # fresh ThreadPoolExecutor per /mywallets call causes under load (a steady
    # stream of short-lived commands can otherwise hit "can't start new thread").
    fut_map = {_WALLET_BALANCE_EXECUTOR.submit(get_wallet_balance, addr, user_id): addr for addr in wallets}
    results = {}
    for fut in concurrent.futures.as_completed(fut_map):
        addr = fut_map[fut]
        try:
            results[addr] = fut.result()
        except Exception as e:
            # One failing wallet used to abort the entire reply — degrade
            # gracefully and keep the rest of the balances visible.
            logger.error(f"Wallet balance failed for {addr[:6]}…: {e}")
            results[addr] = T(user_id, 'tx_error')
    # Render in stored order (as_completed = arrival order, which used to
    # scramble the wallet list on every view).
    lines = []
    for addr in wallets:
        balance_msg = results.get(addr)
        if balance_msg is None:
            continue
        icon = _wallet_chain_icon(addr)
        # Add space between address and balance for better readability
        lines.append(f"{icon} <code>{html.escape(addr)}</code>\n\n {balance_msg}")
    # Apply blockquote formatting
    reply = T(user_id, 'wallets_balances_hdr') + quote("\n\n".join(lines))
    bot.reply_to(message, reply, parse_mode='HTML')
    logger.info(f"User {user_id} viewed wallets with balances")


def _price_line(name_or_sym: str, price: float, chg: float | None = None) -> str:
    """Canonical coin row: '🪙 BTC  $95,432.12  📈 +1.24%'."""
    chg_part = ""
    if chg is not None:
        arrow = '📈' if chg >= 0 else '📉'
        chg_part = f"  {arrow} {chg:+.2f}%"
    return f"🪙 <b>{name_or_sym}</b>  {fmt_price(price)}{chg_part}"


def _coin_price_block(uid, name: str, price_str: str, *,
                      toman_str: str = "", extra_rows=None) -> str:
    """Shared single-coin message body:

        📊 <b>Name</b>
        <blockquote>💵 $95,432
        💰 5,700,000 Toman
        🇹🇷 … EUR …</blockquote>

    Used by plain-text queries, inline mode and price-photo captions so all
    surfaces render the same layout."""
    rows = [f"💵 {price_str}"]
    if toman_str:
        rows.append(toman_str)
    if extra_rows:
        rows.extend(extra_rows)
    return f"📊 <b>{name}</b>\n" + quote("\n".join(rows))


def _build_price_list_message(uid, prices):
    """Build the all-prices message: header + blockquote of coin rows."""
    header = T(uid, 'prices_header')
    body = []
    for code, name in CRYPTO_LIST.items():
        if code == 'telegram-stars':
            continue  # Skip stars in price list
        p = prices.get(code, {})
        price_usd = p.get('usd')
        change = p.get('usd_24h_change')
        if price_usd is None:
            continue
        cache_set(code, price_usd)
        sym = _sym(code)
        # Add space after price, separate percentage change visually
        body.append(_price_line(sym, price_usd, change))
    if not body:
        return header
    return header + "\n" + quote("\n".join(body))


@bot.message_handler(commands=['price'])
@rate_limit_check
def price(message):
    uid_p0 = message.from_user.id
    # Send temporary fetching message
    loading_msg = None
    try:
        loading_msg = bot.send_message(
            message.chat.id,
            add_timestamp(T(uid_p0, 'loading_prices')),
            parse_mode='HTML'
        )
    except Exception:
        pass
    
    bot.send_chat_action(message.chat.id, 'typing')
    ids = ','.join(CRYPTO_LIST.keys())
    prices = _fetch_prices_batch(ids)
    if not prices:
        bot.reply_to(message, T(message.from_user.id, 'price_unavailable'))
        # Don't leave the "Fetching prices..." bubble behind on failure
        if loading_msg:
            try:
                bot.delete_message(loading_msg.chat.id, loading_msg.message_id)
            except Exception:
                pass
        return

    uid_p = message.from_user.id
    text = _build_price_list_message(uid_p, prices)

    kb = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(T(uid_p, 'btn_refresh'), callback_data="refresh_all_prices")
    ]])
    # Edit the loading message with results (keep the refresh button —
    # refresh_all_prices expects this message to already carry it)
    if loading_msg:
        try:
            bot.edit_message_text(
                add_timestamp(text),
                chat_id=loading_msg.chat.id,
                message_id=loading_msg.message_id,
                parse_mode='HTML',
                reply_markup=kb
            )
        except Exception:
            pass
    else:
        bot.reply_to(message, add_timestamp(text), parse_mode='HTML', reply_markup=kb)
    logger.info(f"User {message.from_user.id} requested prices")


# Unified fiat currency command handler
FIAT_COMMANDS = {
    'usd': ('usd_rate', '💵', get_usd_to_irr, 'USD'),
    'try': ('try_rate', '🇹🇷', get_try_to_irr, 'TRY'),
    'eur': ('eur_rate', '🇪🇺', get_eur_to_irr, 'EUR'),
    'gbp': ('gbp_rate', '🇬🇧', get_gbp_to_irr, 'GBP'),
    'aed': ('aed_rate', '🇦🇪', get_aed_to_irr, 'AED'),
    'cny': ('cny_rate', '🇨🇳', get_cny_to_irr, 'CNY'),
}


def _handle_fiat_command(message, cmd_key):
    """Unified handler for fiat currency rate commands."""
    uid = message.from_user.id
    bot.send_chat_action(message.chat.id, 'typing')
    rate_key, emoji, getter, code = FIAT_COMMANDS[cmd_key]
    rate = getter()
    if rate is None:
        bot.reply_to(message, add_timestamp(T(uid, 'rate_unavailable')), parse_mode='HTML')
        return
    rate_str = f"{rate:,.0f}"
    bot.reply_to(
        message,
        add_timestamp(T(uid, rate_key, rate=rate_str)),
        parse_mode='HTML'
    )
    logger.info(f"User {uid} requested {code} → Toman")


@bot.message_handler(commands=['usd'])
@rate_limit_check
@loading_indicator
def usd_command(message):
    _handle_fiat_command(message, 'usd')


@bot.message_handler(commands=['try'])
@rate_limit_check
@loading_indicator
def try_command(message):
    _handle_fiat_command(message, 'try')


@bot.message_handler(commands=['eur'])
@rate_limit_check
@loading_indicator
def eur_command(message):
    _handle_fiat_command(message, 'eur')


@bot.message_handler(commands=['gbp'])
@rate_limit_check
@loading_indicator
def gbp_command(message):
    _handle_fiat_command(message, 'gbp')


@bot.message_handler(commands=['aed'])
@rate_limit_check
@loading_indicator
def aed_command(message):
    _handle_fiat_command(message, 'aed')


@bot.message_handler(commands=['cny'])
@rate_limit_check
@loading_indicator
def cny_command(message):
    _handle_fiat_command(message, 'cny')


def _gold_text(uid, xau, iran) -> str:
    """Shared gold message: global XAU/USD section + Iran Tgju section.

    Used by /gold, plain-text gold queries and inline mode so all three
    surfaces render identically."""
    gram_price_usd = xau / 31.1035
    user_lang = db_get_lang(uid)
    xau_formatted = format_for_locale(format_fiat(Decimal(str(xau))), user_lang)
    gram_usd_formatted = format_for_locale(format_fiat(Decimal(str(gram_price_usd))), user_lang)
    msg = T(uid, 'gold_hdr') + quote(
        T(uid, 'gold_global', xau=xau_formatted, gram=gram_usd_formatted)
    )
    if iran:
        def fmt_toman(v):
            return format_for_locale(f"{v:,}", user_lang) if v else ""
        def iran_line(key, slug):
            price = fmt_toman(iran.get(slug))
            return T(uid, key, price=price) if price else ""
        iran_rows = (
            iran_line('gold_gram18', 'geram18') +
            iran_line('gold_gram24', 'geram24') +
            iran_line('gold_mesghal', 'mesghal') +
            iran_line('gold_emami', 'sekee') +
            iran_line('gold_bahar', 'sekeb') +
            iran_line('gold_nim', 'nim') +
            iran_line('gold_rob', 'rob')
        )
        if iran_rows.strip():
            msg += "\n" + quote(T(uid, 'gold_iran') + iran_rows)
    return msg


def _build_gold_message(uid):
    """Build full gold message: global XAU/USD + Iran tgju prices (Toman)."""
    prices = get_gold_prices()
    xau_price = prices.get('xau')
    if not xau_price:
        return None
    iran = get_iran_gold_prices()
    return _gold_text(uid, xau_price, iran)


@bot.message_handler(commands=['gold'])
@rate_limit_check
@loading_indicator
def gold_command(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    msg = _build_gold_message(uid)
    if not msg:
        bot.reply_to(message, T(uid, 'gold_fetch_fail'))
        return
    bot.reply_to(message, add_timestamp(msg), parse_mode='HTML')
    logger.info(f"User {uid} requested gold prices")


@bot.message_handler(commands=['stars', 'star'])
@rate_limit_check
@loading_indicator
def stars_command(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    stars_price = get_crypto_price('telegram-stars')
    if not stars_price:
        stars_price = float(os.getenv('STARS_PRICE_USD', '0.015'))
    msg = _stars_price_lines(uid, stars_price, 1, T(uid, 'stars_header'))
    kb = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(T(uid, 'stars_calc_btn'), callback_data="stars_calc")
    ]])
    bot.reply_to(message, add_timestamp(msg), parse_mode='HTML', reply_markup=kb)
    logger.info(f"User {uid} requested Stars price")


def _stars_price_lines(uid, stars_price, count=1, header=None, cache_only=False):
    """Build Stars price lines for the given count of stars.

    cache_only=True (inline mode) reads every rate strictly from cache so the
    inline query never blocks on the network; the background warmer keeps
    those values fresh."""
    usd = stars_price * count
    header = header or T(uid, 'stars_result', amount=f"{count:g}")
    if cache_only:
        irr_rate = cache_get('usd_to_irr')
        rates = {code: _cached_toman_rate(code) for code, _, _, _ in _INLINE_FIATS}
        ton_price = get_crypto_price('the-open-network', cache_only=True)
    else:
        irr_rate = get_usd_to_irr()
        rates = {
            'try': get_try_to_irr(),
            'eur': get_eur_to_irr(),
            'gbp': get_gbp_to_irr(),
            'aed': get_aed_to_irr(),
            'cny': get_cny_to_irr(),
        }
        ton_price = get_crypto_price('the-open-network')
    rows = [f"💵 ${usd:.3f} USD"]
    if irr_rate:
        rows.append(f"💰 {format_fiat(Decimal(str(usd * irr_rate)), decimals=0)} {T(uid, 'toman_label')}")
    for code, label, flag, decimals in _INLINE_FIATS:
        val = usd_to_fiat(usd, rates.get(code), decimals=decimals, usd_to_irr=irr_rate)
        if val is not None:
            rows.append(f"{flag} {val:,.{decimals}f} {label}")
    if ton_price:
        rows.append(f"🪙 {format_crypto(Decimal(str(usd / ton_price)))} TON")
    return header + quote("\n".join(rows))


def _show_holding_coin_picker(chat_id, prompt, user_id=0):
    """Show coin picker buttons for add/set holding flows."""
    kb = _coin_picker_keyboard("hpick_", cancel="hpick_cancel", user_id=user_id)
    bot.send_message(chat_id, prompt, parse_mode='HTML', reply_markup=kb)


@bot.message_handler(commands=['set'])
@rate_limit_check
@loading_indicator
def set_holding(message):
    user_id = message.from_user.id
    logger.info(f"User {user_id} initiated set holdings")
    _show_holding_coin_picker(message.chat.id, T(user_id, 'set_holdings_prompt'), user_id)


@bot.message_handler(commands=['holdings'])
@rate_limit_check
@loading_indicator
def holdings(message):
    user_id = message.from_user.id
    saved = db_get_holdings(user_id) or {}
    bot.send_chat_action(message.chat.id, 'typing')
    usd_to_irr = get_usd_to_irr()
    buy_prices = db_get_buy_prices(user_id)
    bot.reply_to(
        message,
        holdings_message_text(saved, usd_to_irr, buy_prices, user_id),
        parse_mode='HTML',
        reply_markup=build_holdings_keyboard(saved, user_id)
    )
    logger.info(f"User {user_id} checked holdings")


@bot.message_handler(commands=['convert'])
@rate_limit_check
@loading_indicator
def convert_cmd(message):
    # Always show the interactive coin picker
    picker_coins = [c for c in CRYPTO_LIST.keys() if c != 'telegram-stars'] + ['usd', 'toman']
    kb = _coin_picker_keyboard("cvt1_", coins=picker_coins)

    uid_cv = message.from_user.id
    msg = bot.reply_to(
        message,
        T(uid_cv, 'convert_step1'),
        parse_mode='HTML',
        reply_markup=kb
    )
    register_panel_owner(message.chat.id, msg.message_id, message.from_user.id)


# ═══════════════════════════════════════════════
# Exchange Price Source
# ═══════════════════════════════════════════════
@bot.message_handler(commands=['setexchange'])
@rate_limit_check
@loading_indicator
def set_exchange_cmd(message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        current = get_user_exchange(uid)
        current_name = EXCHANGE_NAMES.get(current, 'CoinGecko')
        kb = types.InlineKeyboardMarkup(row_width=2)
        btns = [types.InlineKeyboardButton(info['name'], callback_data=f"setex_{e}") for e, info in EXCHANGES.items()]
        btns.append(types.InlineKeyboardButton("CoinGecko", callback_data="setex_coingecko"))
        kb.add(*btns)
        bot.reply_to(
            message,
            T(uid, 'exch_current', name=current_name) + "\n\n" + T(uid, 'exch_prompt'),
            parse_mode='HTML', reply_markup=kb
        )
        return
    exch = args[1].strip().lower()
    if exch not in EXCHANGES and exch != 'coingecko':
        bot.reply_to(
            message,
            T(uid, 'exch_unknown', list=f"coingecko, {', '.join(EXCHANGES.keys())}"),
            parse_mode='HTML'
        )
        return
    set_user_exchange(uid, exch)
    name = EXCHANGE_NAMES.get(exch, 'CoinGecko')
    bot.reply_to(message, T(uid, 'exch_set', name=name), parse_mode='HTML')
    logger.info(f"User {uid} set exchange to {exch}")


# ═══════════════════════════════════════════════
# Better Charts
# ═══════════════════════════════════════════════
CHART_DAYS = {'7d': 7, '30d': 30, '90d': 90, '1y': 365}


@bot.message_handler(commands=['chart'])
@rate_limit_check
@loading_indicator
def chart_cmd(message):
    uid = message.from_user.id

    # Always show the coin picker (buttons)
    kb = _coin_picker_keyboard("chr1_")
    msg = bot.reply_to(
        message,
        T(uid, 'chart_pick_coin'),
        parse_mode='HTML',
        reply_markup=kb
    )
    register_panel_owner(message.chat.id, msg.message_id, uid)




# ═══════════════════════════════════════════════
# Trending & Market Movers
# ═══════════════════════════════════════════════
TRENDING_CACHE_TTL = 300


def _fetch_trending():
    cached = cache_get('trending_coins')
    if cached:
        return cached
    try:
        resp = session.get('https://api.coingecko.com/api/v3/search/trending', timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json().get('coins', [])
        result = []
        for item in data[:10]:
            coin = item.get('item', {})
            result.append({
                'id': coin.get('id'),
                'symbol': coin.get('symbol', '').upper(),
                'name': coin.get('name'),
                'market_cap_rank': coin.get('market_cap_rank'),
                'price_btc': coin.get('price_btc'),
                'thumb': coin.get('thumb'),
            })
        cache_set('trending_coins', result, ttl=TRENDING_CACHE_TTL)
        return result
    except Exception as e:
        logger.error(f"Trending fetch failed: {e}")
        return None


def _fetch_gainers_losers():
    cached = cache_get('gainers_losers')
    if cached:
        return cached
    try:
        resp = session.get(
            'https://api.coingecko.com/api/v3/coins/markets'
            '?vs_currency=usd&order=volume_desc&per_page=50&page=1'
            '&sparkline=false&price_change_percentage=24h',
            timeout=10
        )
        if resp.status_code != 200:
            return None
        coins = resp.json()
        coins = [c for c in coins if c.get('price_change_percentage_24h') is not None]
        coins.sort(key=lambda c: c['price_change_percentage_24h'], reverse=True)
        data = {
            'gainers': coins[:5],
            'losers': coins[-5:][::-1],
        }
        cache_set('gainers_losers', data, ttl=TRENDING_CACHE_TTL)
        return data
    except Exception as e:
        logger.error(f"Gainers/losers fetch failed: {e}")
        return None


@bot.message_handler(commands=['trending'])
@rate_limit_check
@loading_indicator
def trending_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    data = _fetch_trending()
    if not data:
        bot.reply_to(message, T(uid, 'trending_fail'))
        return
    lines = []
    for i, coin in enumerate(data, 1):
        rank = coin.get('market_cap_rank')
        rank_str = f"#{rank}" if rank else "-"
        lines.append(f"{i}. <b>{coin['name']}</b> ({coin['symbol']})  ─  Rank {rank_str}")
    # Apply blockquote formatting
    text = add_timestamp("🔥 <b>Trending on CoinGecko</b>\n" + quote("\n".join(lines)))
    bot.reply_to(message, text, parse_mode='HTML')


@bot.message_handler(commands=['gainers', 'losers'])
@rate_limit_check
@loading_indicator
def gainers_losers_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    data = _fetch_gainers_losers()
    if not data:
        bot.reply_to(message, T(uid, 'movers_fail'))
        return
    cmd = message.text.strip().lower()
    show_gainers = cmd.startswith('/gainers')

    coins = data['gainers'] if show_gainers else data['losers']
    emoji = '📈' if show_gainers else '📉'
    title = "Top Gainers (24h)" if show_gainers else "Top Losers (24h)"
    lines = []
    for c in coins:
        chg = c['price_change_percentage_24h']
        arrow = '📈' if chg >= 0 else '📉'
        price = c.get('current_price')
        price_str = fmt_price(price) if price else '-'
        lines.append(f"{arrow} <b>{c['symbol'].upper()}</b>  {price_str}  <i>{chg:+.2f}%</i>")
    # Apply blockquote formatting
    text = add_timestamp(f"{emoji} <b>{title}</b>\n" + quote("\n".join(lines)))
    bot.reply_to(message, text, parse_mode='HTML')


def _safe_inline(handler):
    """Guarantee an inline query always gets an answer, even if a provider
    call raises mid-query. Telegram silently drops un-answered inline queries,
    which makes inline mode look broken."""
    @functools.wraps(handler)
    def wrapper(inline_query: types.InlineQuery):
        try:
            return handler(inline_query)
        except Exception as e:
            logger.error(f"Inline query handler crashed: {e}", exc_info=True)
            try:
                _uid = inline_query.from_user.id
                bot.answer_inline_query(
                    inline_query.id,
                    [types.InlineQueryResultArticle(
                        id="inline_error",
                        title=T(_uid, 'inline_tips_title'),
                        description=T(_uid, 'inline_tips_desc'),
                        input_message_content=types.InputTextMessageContent(
                            add_timestamp(T(_uid, 'inline_tips_body')),
                            parse_mode='HTML'
                        )
                    )],
                    cache_time=5,
                    is_personal=True,
                    switch_pm_text=T(_uid, 'inline_help_button'),
                    switch_pm_parameter='inline_help'
                )
            except Exception:
                logger.warning("Inline fallback answer failed", exc_info=True)
    return wrapper


_INLINE_FIATS = (
    ('try', 'TRY', '🇹🇷', 0),
    ('eur', 'EUR', '🇪🇺', 2),
    ('gbp', 'GBP', '🇬🇧', 2),
    ('aed', 'AED', '🇦🇪', 2),
    ('cny', 'CNY', '🇨🇳', 2),
)


def _cached_toman_rate(code: str):
    """Toman per 1 unit of `code`, from cache only (never touches the network).

    Inline queries must be answered within a few seconds, so they only read
    values the background warmer keeps fresh.
    """
    code = code.lower()
    if code == 'toman':
        return 1
    if code == 'usd':
        return cache_get('usd_to_irr')
    return cache_get(f'{code}_irr')


def _inline_fiat_lines(usd_value, usd_to_irr) -> list[str]:
    """Extra 'TRY/EUR/GBP/AED/CNY' lines for an inline result, cache-only."""
    lines = []
    if not usd_to_irr:
        return lines
    for code, label, flag, decimals in _INLINE_FIATS:
        rate = _cached_toman_rate(code)
        val = usd_to_fiat(usd_value, rate, decimals=decimals, usd_to_irr=usd_to_irr)
        if val is not None:
            lines.append(f"{flag} {val:,.{decimals}f} {label}")
    return lines


_inline_prewarm_running = threading.Event()


def _spawn_inline_prewarm():
    """Run _prewarm_inline_caches in its own thread (each throttled fetch can
    sleep ~3s, which must not stall the alert/digest scheduler). One at a time."""
    if _inline_prewarm_running.is_set():
        return

    def _run():
        try:
            _prewarm_inline_caches()
        finally:
            _inline_prewarm_running.clear()

    _inline_prewarm_running.set()
    try:
        threading.Thread(target=_run, daemon=True, name="InlinePrewarm").start()
    except Exception:
        _inline_prewarm_running.clear()


def _prewarm_inline_caches():
    """Keep every value the inline handler reads (cache-only) populated."""
    for fn in (get_usd_to_irr, get_try_to_irr, get_eur_to_irr, get_gbp_to_irr,
               get_aed_to_irr, get_cny_to_irr, get_gold_prices,
               get_iran_gold_prices, _fetch_fear_greed, _fetch_coingecko_global):
        try:
            fn()
        except Exception as e:
            logger.debug(f"Inline pre-warm {fn.__name__} failed: {e}")
    try:
        get_crypto_price('telegram-stars')
    except Exception:
        pass


@bot.inline_handler(lambda query: True)
@_safe_inline
def inline_query_handler(inline_query):
    q     = inline_query.query.strip()
    ql    = q.lower().strip()
    uid   = inline_query.from_user.id
    results = []

    def article(id_, title, desc, text, html=False):
        return types.InlineQueryResultArticle(
            id=id_, title=title, description=desc,
            input_message_content=types.InputTextMessageContent(
                add_timestamp(text), parse_mode='HTML' if html else None
            )
        )

    # ── 1. Transaction hash or link - detect chain from URL ──────────────
    tronscan_match = re.match(r'https?://tronscan\.org/#/transaction/([A-Fa-f0-9]{64})', q, re.IGNORECASE)
    tonviewer_match = re.match(r'https?://tonviewer\.com/transaction/([A-Fa-f0-9]{64})', q, re.IGNORECASE)
    tonscan_match = re.match(r'https?://tonscan\.org/tx/([A-Fa-f0-9]{64})', q, re.IGNORECASE)
    bare_hash_match = re.match(r'^[A-Fa-f0-9]{64}$', q)

    tx_hash = None
    detected_chain = None  # 'tron', 'ton', or None for bare hash

    if tronscan_match:
        tx_hash = tronscan_match.group(1)
        detected_chain = 'tron'
    elif tonviewer_match:
        tx_hash = tonviewer_match.group(1)
        detected_chain = 'ton'
    elif tonscan_match:
        tx_hash = tonscan_match.group(1)
        detected_chain = 'ton'
    elif bare_hash_match:
        tx_hash = q
        detected_chain = None  # Unknown - will try both

    if tx_hash:
        if detected_chain == 'tron':
            # TRON-specific URL - only try TRON
            try:
                tx = get_tron_transaction_details_inline(tx_hash, uid)
                # Result already contains the Tronscan link
                results.append(article(
                    "txhash", "TRON Transaction", "Tap to share TX details",
                    tx, html=True
                ))
            except Exception as e:
                logger.error(f"Inline TRON tx error: {e}")
        elif detected_chain == 'ton':
            # TON-specific URL - only try TON
            try:
                tx = get_ton_transaction_details_inline(tx_hash, uid)
                results.append(article(
                    "ton_tx", "TON Transaction", "Tap to share TX details",
                    tx, html=True
                ))
            except Exception as e:
                logger.error(f"Inline TON tx error: {e}")
        else:
            # Bare hash - try both and show both if found
            tron_result = None
            ton_result = None
            try:
                tron_result = get_tron_transaction_details_inline(tx_hash, uid)
            except Exception as e:
                logger.error(f"Inline TRON tx bare error: {e}")
            try:
                ton_result = get_ton_transaction_details_inline(tx_hash, uid)
            except Exception as e:
                logger.error(f"Inline TON tx bare error: {e}")
            
            tron_ok = tron_result and "not found" not in tron_result.lower() and "error" not in tron_result.lower() and "timeout" not in tron_result.lower()
            ton_ok = ton_result and "not found" not in ton_result.lower() and "error" not in ton_result.lower() and "timeout" not in ton_result.lower()
            
            if tron_ok:
                results.append(article(
                    "txhash_tron", "TRON Transaction", "Tap to share TX details",
                    tron_result, html=True
                ))
                # No group cache: inline queries have no chat to dedup against
            if ton_ok:
                results.append(article(
                    "txhash_ton", "TON Transaction", "Tap to share TX details",
                    ton_result, html=True
                ))
                # No group cache: inline queries have no chat to dedup against
            if not tron_ok and not ton_ok:
                # Neither found - show a helpful message
                results.append(article(
                    "txhash_none", "Transaction Not Found",
                    f"Hash: {tx_hash[:16]}...",
                    "❌ <b>Transaction Not Found</b>\n"
                    + quote(
                        "Not found on TRON or TON networks.\n"
                        f"📝 <code>{html.escape(tx_hash)}</code>"
                    ),
                    html=True
                ))

    # ── 2. TRON wallet address (34 chars) ────────────────────────────
    elif re.match(r'^[A-Za-z0-9]{34}$', q) and is_valid_tron_address(q):
        try:
            bal = get_tron_wallet_trx(q, uid)
            short = f"{q[:8]}...{q[-6:]}"
            # No group cache: inline queries have no chat to dedup against
            results.append(article(
                "wallet_addr", f"TRON Wallet {short}", bal,
                T(uid, 'wallet_header_tron') + quote(f"<code>{q}</code>\n{bal}"),
                html=True
            ))
        except Exception:
            results.append(article(
                "wallet_addr", f"TRON Wallet", q,
                T(uid, 'wallet_header_tron') + quote(f"<code>{q}</code>"),
                html=True
            ))

    # ── 3. TON wallet address (48 chars, EQ/UQ) ────────────────────
    elif len(q) == 48 and q[:2] in ('EQ', 'UQ') and is_valid_ton_address(q):
        try:
            bal = get_ton_wallet_balance(q, uid)
            short = f"{q[:8]}...{q[-6:]}"
            # No group cache: inline queries have no chat to dedup against
            results.append(article(
                "ton_wallet", f"TON Wallet {short}", bal,
                T(uid, 'wallet_header_ton') + quote(f"<code>{q}</code>\n{bal}"),
                html=True
            ))
        except Exception:
            results.append(article(
                "ton_wallet", f"TON Wallet", q,
                T(uid, 'wallet_header_ton') + quote(f"<code>{q}</code>"),
                html=True
            ))

    else:
        _irr_val = None
        def _irr():
            nonlocal _irr_val
            if _irr_val is None:
                _irr_val = cache_get('usd_to_irr')  # cache only - never blocks on network
            return _irr_val

        # ── 4. USD / Toman rate ───────────────────────────────────────
        usd_m = re.match(
            r'^([\d.,۰-۹٬٫]+)?\s*(usd|\$|dollar)(\s+(to|to)\s*(toman|تومان|تومن|irr))?$',
            ql
        )
        if usd_m:
            irr = _irr()
            if irr is not None:
                amt_str = usd_m.group(1) if usd_m.group(1) else "1"
                amt = parse_number(amt_str)
                if amt is None:
                    amt = Decimal('1')
                
                val = amt * Decimal(str(irr))
                lbl = T(uid, 'inline_usd_toman')
                
                user_lang = db_get_lang(uid)
                amt_formatted = format_fiat(amt)
                val_formatted = format_fiat(val, decimals=0)
                amt_formatted = format_for_locale(amt_formatted, user_lang)
                val_formatted = format_for_locale(val_formatted, user_lang)
                
                toman_label = T(uid, 'toman_label')
                desc = f"{amt_formatted} USD = {val_formatted} {toman_label}"
                # Same blockquote line as the 'usd' text command
                txt = T(uid, 'usd_conversion', amount=amt_formatted, result=val_formatted)
                results.append(article(
                    "usd_toman", lbl, desc, txt, html=True
                ))

        # ── 5. Toman → USD ────────────────────────────────────────────
        toman_m = re.match(
            r'^([\d.,۰-۹٬٫]+)\s*(toman|تومان|تومن|ریال|irr)(\s+(to|به)\s*(usd|\$))?$',
            ql
        )
        if toman_m:
            irr = _irr()
            if irr is not None:
                amt = parse_number(toman_m.group(1))
                if amt is None:
                    amt = Decimal('0')
                
                val = amt / Decimal(str(irr))
                lbl = T(uid, 'inline_toman_usd')
                
                user_lang = db_get_lang(uid)
                amt_formatted = format_fiat(amt, decimals=0)
                val_formatted = format_fiat(val)
                amt_formatted = format_for_locale(amt_formatted, user_lang)
                val_formatted = format_for_locale(val_formatted, user_lang)
                
                toman_label = T(uid, 'toman_label')
                desc = f"{amt_formatted} {toman_label} = ${val_formatted}"
                results.append(article(
                    "toman_usd", lbl, desc,
                    f"💵 <b>{lbl}</b>\n" + quote(desc), html=True
                ))

        # ── 6. Conversion (10 btc to eth / 100 usd trx) - FASTER ─────
        # Support flexible number formats and both "10 btc to eth" and "10btc to eth"
        conv_m = re.match(
            r'^([\d.,۰-۹٬٫]+)\s*(\w+|تومان|تومن)\s+(?:to\s+|به\s+)?(\w+|تومان|تومن)$', ql
        )
        if conv_m:
            amt = parse_number(conv_m.group(1))
            if amt is None:
                amt = Decimal('0')
            
            # Normalize currency keywords (support تومن alongside تومان)
            src_raw = conv_m.group(2)
            dst_raw = conv_m.group(3)
            src_raw = src_raw.replace('تومن', 'تومان')
            dst_raw = dst_raw.replace('تومن', 'تومان')
            
            src  = detect_currency(src_raw, check_u_alias=True)
            dst  = detect_currency(dst_raw, check_u_alias=True)
            
            # Telegram Stars conversions are not supported
            if src and dst and src != dst and 'telegram-stars' not in (src, dst):
                result_val = None
                
                # FASTER: Pre-fetch prices, don't call convert_amount (which re-fetches)
                # Use Decimal for all arithmetic to avoid precision loss
                if src in CRYPTO_LIST and dst in CRYPTO_LIST:
                    p_src = get_crypto_price(src, cache_only=True)
                    p_dst = get_crypto_price(dst, cache_only=True)
                    if p_src and p_dst:
                        result_val = amt * Decimal(str(p_src)) / Decimal(str(p_dst))
                elif src == 'usd' and dst in CRYPTO_LIST:
                    p_dst = get_crypto_price(dst, cache_only=True)
                    if p_dst:
                        result_val = amt / Decimal(str(p_dst))
                elif src in CRYPTO_LIST and dst == 'usd':
                    p_src = get_crypto_price(src, cache_only=True)
                    if p_src:
                        result_val = amt * Decimal(str(p_src))
                elif _irr() is not None and src == 'toman' and dst == 'usd':
                    result_val = amt / Decimal(str(_irr()))
                elif _irr() is not None and src == 'usd' and dst == 'toman':
                    result_val = amt * Decimal(str(_irr()))
                elif _irr() is not None and src == 'toman' and dst in CRYPTO_LIST:
                    p_dst = get_crypto_price(dst, cache_only=True)
                    if p_dst:
                        result_val = amt / Decimal(str(_irr())) / Decimal(str(p_dst))
                elif _irr() is not None and src in CRYPTO_LIST and dst == 'toman':
                    p_src = get_crypto_price(src, cache_only=True)
                    if p_src:
                        result_val = amt * Decimal(str(p_src)) * Decimal(str(_irr()))
                # Fiat-to-fiat conversions (e.g., TRY -> Toman, EUR -> TRY, USD -> EUR, etc.)
                elif src in FIAT_ALIASES.values() and dst in FIAT_ALIASES.values():  # cache-only rates
                    src_to_toman = _cached_toman_rate(src)
                    dst_from_toman = _cached_toman_rate(dst)
                    
                    if src_to_toman and dst_from_toman:
                        # Convert: amt * (src_to_toman / dst_from_toman)
                        result_val = amt * (Decimal(str(src_to_toman)) / Decimal(str(dst_from_toman)))
                
                if result_val is not None:
                    toman_lbl = T(uid, 'toman_label')
                    user_lang = db_get_lang(uid)
                    
                    # Get clean symbols (not IDs like 'the-open-network')
                    if src in ('usd', 'toman'):
                        src_sym = 'USD' if src == 'usd' else toman_lbl
                    elif src in CRYPTO_LIST:
                        # Extract symbol from "🪙 Name (SYMBOL)" format
                        src_sym = _sym(src)
                    else:
                        src_sym = src.upper()
                    
                    if dst in ('usd', 'toman'):
                        dst_sym = 'USD' if dst == 'usd' else toman_lbl
                    elif dst in CRYPTO_LIST:
                        # Extract symbol from "🪙 Name (SYMBOL)" format
                        dst_sym = _sym(dst)
                    else:
                        dst_sym = dst.upper()
                    
                    # Format with proper number handling
                    amt_formatted = format_crypto(amt) if amt < 1000 else format_fiat(amt)
                    
                    if dst in ('usd', 'toman'):
                        result_formatted = format_fiat(Decimal(str(result_val)))
                    else:
                        result_formatted = format_crypto(Decimal(str(result_val)))
                    
                    # Apply locale
                    amt_formatted = format_for_locale(amt_formatted, user_lang)
                    result_formatted = format_for_locale(result_formatted, user_lang)
                    
                    # Main conversion row; fiat destinations also get the
                    # USD/Toman equivalent as a second row.
                    conv_line = f"{amt_formatted} {src_sym} = {result_formatted} {dst_sym}"
                    rows = [conv_line]
                    usd_to_irr = _irr()
                    if dst == 'usd' and usd_to_irr:
                        rows.append(
                            f"💰 {result_val:,.2f} USD = "
                            f"{result_val * Decimal(str(usd_to_irr)):,.0f} {toman_lbl}"
                        )
                    elif dst == 'toman' and usd_to_irr:
                        usd_val = result_val / Decimal(str(usd_to_irr))
                        rows.append(
                            f"💰 {usd_val:,.2f} USD = {result_val:,.0f} {toman_lbl}"
                        )
                    desc = conv_line if len(rows) == 1 else conv_line + "\n" + rows[1]

                    results.append(article(
                        "convert", f"{src_sym} → {dst_sym}", desc,
                        f"{T(uid, 'inline_conv_header')}\n" + quote("\n".join(rows)),
                        html=True
                    ))


        # ── 7. Amount + single coin (10 trx / 0.5 btc / 10u) ─────────
        # Support both "10 trx" and "10trx" (no space), and "10u" for USDT
        amt_m = re.match(r'^(\d+(?:\.\d+)?)\s*(\w+)$', ql)
        if amt_m:
            amt = Decimal(amt_m.group(1))
            cur = detect_currency(amt_m.group(2), check_u_alias=True)
            if cur and cur in CRYPTO_LIST:
                p = get_crypto_price(cur, cache_only=True)
                if p:
                    v_usd = amt * Decimal(str(p))
                    v_usd_f = float(v_usd)
                    irr_v = _irr()
                    v_irr_f = float(v_usd * Decimal(str(irr_v))) if irr_v else 0
                    amt_f = float(amt)
                    name  = _sym(cur)
                    toman_lbl = T(uid, 'toman_label')
                    desc  = f"${v_usd_f:,.2f} | {v_irr_f:,.0f} {toman_lbl}" if irr_v else f"${v_usd_f:,.2f}"
                    # Same rows as the '10 trx' text reply: 💵 price, 💰 toman, 🇹🇷🇪🇺… fiat
                    rows = [f"💵 {fmt_price(v_usd_f)}"]
                    if irr_v:
                        rows.append(f"💰 {v_irr_f:,.0f} {toman_lbl}")
                    rows.extend(_inline_fiat_lines(v_usd_f, irr_v))
                    msg_text = f"💰 <b>{amt_f:,.6g} {name}</b>\n" + quote("\n".join(rows))
                    results.append(article(
                        "amt_coin", f"{amt_f:,.6g} {name}",
                        desc,
                        msg_text, html=True
                    ))

        # ── 8. Single crypto name (btc / eth / trx / u …) ────────────
        crypto = detect_currency(ql, check_u_alias=True)
        if crypto and crypto in CRYPTO_LIST:
            p = get_crypto_price(crypto, cache_only=True)
            if p:
                irr_v = _irr()
                name  = CRYPTO_LIST[crypto]
                toman_lbl = T(uid, 'toman_label')

                # Shared price block (same layout as the text reply)
                if irr_v:
                    toman_price = p * irr_v
                    desc_text = f"{fmt_price(p)} | {toman_price:,.0f} {toman_lbl}"
                else:
                    toman_price = None
                    desc_text = fmt_price(p)

                msg_text = _coin_price_block(
                    uid, name, fmt_price(p),
                    toman_str=f"💰 {toman_price:,.0f} {toman_lbl}" if toman_price is not None else "",
                    extra_rows=_inline_fiat_lines(p, irr_v),
                )
                results.append(article(
                    "crypto_price", f"{name} Price",
                    desc_text,
                    msg_text,
                    html=True
                ))

        # ── 9. "price" keyword → full price list ─────────────────────
        if ql in ('price', 'prices', 'قیمت', 'قیمت‌ها'):
            try:
                # Serve from warm cache first so inline answers stay fast;
                # only hit the network when nothing is cached yet.
                prices = {}
                for _code in CRYPTO_LIST:
                    _p = cache_get(_code)
                    if _p is not None:
                        prices[_code] = {'usd': _p}
                if not prices:
                    prices = _fetch_prices_coingecko(','.join(CRYPTO_LIST.keys()))
                if prices:
                    txt = _build_price_list_message(uid, prices)
                    results.append(article(
                        "all_prices", "All Crypto Prices",
                        "Tap to share full price list", txt, html=True
                    ))
            except Exception:
                pass

        # ── 10. Gold prices ────────────────────────────────────────────
        if ql in ('gold', 'gold price', 'طلا', 'قیمت طلا'):
            _xau_cached = cache_get('gold_xau')
            gold = {'xau': _xau_cached} if _xau_cached is not None else get_gold_prices()
            if gold and 'xau' in gold:
                xau = gold['xau']
                iran = cache_get('iran_gold')  # cache only - never blocks inline
                # Shared gold text (same layout as /gold and the text reply)
                txt = _gold_text(uid, xau, iran)
                results.append(article("gold", "Gold Price", f"XAU/USD: ${xau:,.2f}", txt, html=True))

        # ── 11. Telegram Stars ─────────────────────────────────────────
        if ql in ('star', 'stars', 'telegram stars', 'استار', 'استارز', 'ستاره'):
            stars_price = get_crypto_price('telegram-stars', cache_only=True)
            if stars_price:
                # Shared stars block (same layout as /stars)
                txt = _stars_price_lines(uid, stars_price, 1, T(uid, 'stars_header'), cache_only=True)
                results.append(article("stars", "Telegram Stars", f"${stars_price:.3f}", txt, html=True))

        # ── 12. Market (Fear & Greed) ──────────────────────────────────
        if ql in ('market', 'fear', 'greed', 'fear and greed', 'fear & greed', 'بازار', 'ترس و طمع'):
            try:
                # Serve from cache first so inline stays fast; the shared
                # helpers cache their results after every successful fetch.
                g = cache_get('cg_global') or _fetch_coingecko_global()
                if g:
                    mcap = g.get('total_market_cap', {}).get('usd', 0)
                    vol = g.get('total_volume', {}).get('usd', 0)
                    btc_dom = g.get('market_cap_percentage', {}).get('btc', 0)
                    eth_dom = g.get('market_cap_percentage', {}).get('eth', 0)
                    coins = g.get('active_cryptocurrencies', 0)
                    chg24 = g.get('market_cap_change_percentage_24h_usd', 0)
                    arrow = '📈' if chg24 >= 0 else '📉'
                    fg_data = cache_get('fng')
                    if not fg_data:
                        try:
                            fg_data = _fetch_fear_greed()
                        except Exception:
                            fg_data = None
                    # Same layout as /market (shared T-keys + _fg_block)
                    txt = (
                        T(uid, 'market_header') +
                        quote(
                            T(uid, 'market_mcap', mcap=f"${mcap/1e12:.2f}T", arrow=arrow, chg=f"{chg24:+.1f}") +
                            T(uid, 'market_vol',  vol=f"${vol/1e9:.1f}B") +
                            T(uid, 'market_dom',  btc=f"{btc_dom:.1f}", eth=f"{eth_dom:.1f}") +
                            T(uid, 'market_coins', coins=f"{coins:,}") +
                            T(uid, 'market_fg', fg=_fg_block(uid, fg_data))
                        )
                    )
                    results.append(article("market", "Market Overview", f"${mcap/1e12:.2f}T · {chg24:+.1f}%", txt, html=True))
            except Exception:
                pass

        # ── 13. TRY to Toman rate ──────────────────────────────────────
        if ql in ('try', 'tl', 'lira', 'ترک', 'لیر'):
            try_rate = cache_get('try_irr') or get_try_to_irr()
            if try_rate:
                # Same blockquote line as the 'try' text reply
                txt = T(uid, 'try_rate', rate=f"{try_rate:,}")
                toman_lbl = T(uid, 'toman_label')
                results.append(article("try_rate", f"1 TRY = {try_rate:,} {toman_lbl}", f"TRY → {toman_lbl}", txt, html=True))

        # ── 13b. EUR / GBP / AED / CNY to Toman (TRY is handled above in ── 13)
        fx_inline = [
            (('eur', 'euro', '€'), 'EUR', get_eur_to_irr),
            (('gbp', 'pound', '£'), 'GBP', get_gbp_to_irr),
            (('aed', 'dirham', 'درهم'), 'AED', get_aed_to_irr),
            (('cny', 'yuan', 'یوآن'), 'CNY', get_cny_to_irr),
        ]
        for fx_keys, fx_code, fx_getter in fx_inline:
            if ql in fx_keys:
                fx_rate = cache_get(f"{fx_code.lower()}_irr") or fx_getter()
                if fx_rate:
                    toman_lbl = T(uid, 'toman_label')
                    # Same conversion line as the fx text replies
                    result_formatted = format_for_locale(
                        format_fiat(Decimal(str(fx_rate)), decimals=0), db_get_lang(uid)
                    )
                    txt = T(uid, f"{fx_code.lower()}_conversion",
                            amount="1", result=result_formatted)
                    results.append(article(
                        f"{fx_code.lower()}_rate",
                        f"1 {fx_code} = {fx_rate:,} {toman_lbl}",
                        f"{fx_code} → {toman_lbl}", txt, html=True
                    ))

        # ── 14. Holdings / Portfolio ────────────────────────────────────
        if ql in ('holdings', 'portfolio', 'port', 'پرتفو', 'دارایی'):
            saved = db_get_holdings(uid) or {}
            if saved:
                # Same layout as /holdings (cache-only prices for inline speed)
                irr_v = _irr()
                buy_prices = db_get_buy_prices(uid)
                rows = []
                total = 0.0
                for symbol, amount in saved.items():
                    cid = detect_currency(symbol.lower())
                    if not cid:
                        continue
                    p = get_crypto_price(cid, cache_only=True)
                    if p:
                        val = amount * p
                        total += val
                        line = f"🪙 <b>{symbol}</b>  {amount:,.6g} · <i>{fmt_price(val)}</i>"
                        buy = buy_prices.get(symbol.upper())
                        if buy and buy > 0:
                            pnl_usd = (p - buy) * amount
                            pnl_pct = ((p - buy) / buy) * 100
                            p_arrow = "📈" if pnl_usd >= 0 else "📉"
                            p_sign = "+" if pnl_usd >= 0 else ""
                            line += f"\n   {p_arrow} {p_sign}${pnl_usd:,.2f} ({p_sign}{pnl_pct:.1f}%)  {T(uid, 'buy_at', price=fmt_price(buy))}"
                        rows.append(line)
                    else:
                        rows.append(f"🪙 <b>{symbol}</b>  {amount:,.6g} · <i>{T(uid, 'price_unavail_short')}</i>")
                total_line = _portfolio_total_line(uid, total, irr_v)
                txt = T(uid, 'portfolio_header') + (quote("\n".join(rows)) if rows else "") + total_line
                results.append(article("holdings", "Portfolio", f"Total: {fmt_price(total)}", txt, html=True))

        # ── 15. Alerts ──────────────────────────────────────────────────
        if ql in ('alerts', 'alert', 'my alerts', 'my alert', 'هشدار', 'هشدارها'):
            alerts = db_get_alerts(uid)
            if alerts:
                # Same layout as the /alerts panel (no delete buttons inline)
                header = T(uid, 'alerts_header', count=len(alerts), max=MAX_ALERTS_PER_USER)
                body = []
                for a in alerts:
                    arrow = '📈' if a['direction'] == 'above' else '📉'
                    direction_word = T(uid, 'above_word') if a['direction'] == 'above' else T(uid, 'below_word')
                    p = get_crypto_price(a['crypto_id'], cache_only=True)
                    if p:
                        pct_str = f"{abs((a['target_price'] - p) / p) * 100:.1f}"
                        dist = f"  <i>{T(uid, 'away_pct', pct=pct_str)}</i>"
                    else:
                        dist = ""
                    body.append(f"{arrow} <b>{a['symbol']}</b> {direction_word} <b>{fmt_price(a['target_price'])}</b>{dist}")
                txt = header + (quote("\n".join(body)) if body else "")
                results.append(article("alerts", f"Alerts ({len(alerts)})", f"{len(alerts)} active", txt, html=True))
            else:
                results.append(article("no_alerts", T(uid, 'no_alerts_simple'), "", T(uid, 'no_alerts_simple'), html=True))

        # ── 16. Math expression ───────────────────────────────────────
        if re.match(r'^[\d+\-*/().%\s]+$', q) and any(c in q for c in '+-*/'):
            res = evaluate_math(q, uid)
            if res and not res.startswith('❌'):
                results.append(article(
                    "math", "Calculator", res.replace('✅', '').strip(), res
                ))

        # ── 11. Wallets ───────────────────────────────────────────────
        # Check for address-only mode first (no API call)
        addr_only_trigger = any(w in ql for w in (
            'wallets addr', 'wallet addr', 'ولت آدرس', 'آدرس ولت',
            'wallets address', 'wallet address', 'آدرس کیف پول'
        ))
        balance_trigger = any(w in ql for w in (
            'wallet', 'wallets', 'mywallet', 'ولت', 'کیف پول', 'کیف‌پول'
        ))

        if addr_only_trigger or balance_trigger:
            wallets = db_get_wallets(uid)
            if not wallets:
                results.append(article(
                    "no_wallets", T(uid, 'no_wallets_inline_title'),
                    T(uid, 'no_wallets_inline_desc'),
                    T(uid, 'no_wallets_inline'),
                    html=True
                ))
            else:
                for i, addr in enumerate(wallets):
                    short = f"{addr[:8]}...{addr[-6:]}"
                    wlbl = T(uid, 'wallet_label', n=i+1)
                    if addr_only_trigger:
                        # Address-only mode - instant, no API call
                        results.append(article(
                            f"waddr_{i}", f"📋 {wlbl}  {short}",
                            T(uid, 'wallet_addr_only_desc'),
                            f"{EMOJIS['wallet']} <b>{wlbl}</b>\n" + quote(f"<code>{addr}</code>"),
                            html=True
                        ))
                    else:
                        # Show both: address-only first, then with balance
                        results.append(article(
                            f"waddr_{i}", f"📋 {wlbl} - {T(uid, 'wallet_addr_only_label')}",
                            short,
                            f"{EMOJIS['wallet']} <b>{wlbl}</b>\n" + quote(f"<code>{addr}</code>"),
                            html=True
                        ))
                        try:
                            bal = get_wallet_balance(addr, uid)
                            results.append(article(
                                f"wbal_{i}", f"👛 {wlbl} - {T(uid, 'wallet_with_balance_label')}",
                                bal,
                                f"{EMOJIS['wallet']} <b>{wlbl}</b>\n" + quote(f"<code>{addr}</code>\n{bal}"),
                                html=True
                            ))
                        except Exception:
                            pass

    # ── Fallback ──────────────────────────────────────────────────────
    if not results:
        results.append(article(
            "help_inline",
            T(uid, 'inline_tips_title'),
            T(uid, 'inline_tips_desc'),
            T(uid, 'inline_tips_body'),
            html=True
        ))

    # Add help button (switch_pm) for popup tip
    try:
        bot.answer_inline_query(
            inline_query.id, 
            results, 
            cache_time=30, 
            is_personal=True,
            switch_pm_text=T(uid, 'inline_help_button'),
            switch_pm_parameter='inline_help'
        )
    except Exception:
        logger.warning("answer_inline_query failed", exc_info=True)  # e.g. query expired



# ─────────────────────────────────────────────
# Price alerts
# ─────────────────────────────────────────────


@bot.message_handler(commands=['alert'])
@rate_limit_check
@loading_indicator
def alert_cmd(message):
    user_id = message.from_user.id
    # Normalize Persian/Arabic digits so "۱۰۰۰۰۰" works the same as "100000"
    # (the text-state paths already do this; the direct command form did not).
    parts = _normalize_persian(message.text).strip().split()

    # Direct: /alert BTC 100000  or  /alert BTC above 100000
    if len(parts) >= 3:
        symbol_raw  = parts[1]
        crypto_id   = detect_currency(symbol_raw.lower())
        if not crypto_id or crypto_id not in CRYPTO_LIST:
            bot.reply_to(message, T(message.from_user.id, 'unknown_coin', sym=symbol_raw), parse_mode='HTML')
            return
        if len(parts) == 3:
            direction_raw, price_raw = 'cross', parts[2]
        else:
            direction_raw, price_raw = parts[2].lower(), parts[3]
        _finalize_alert(message, user_id, crypto_id, direction_raw, price_raw)
        return

    # No args - show coin picker
    existing = db_get_alerts(user_id)
    if len(existing) >= MAX_ALERTS_PER_USER:
        bot.reply_to(message, T(message.from_user.id, 'alert_limit', max=MAX_ALERTS_PER_USER), parse_mode='HTML')
        return

    kb = _coin_picker_keyboard("alrt1_", cancel=True, user_id=user_id)

    msg = bot.reply_to(
        message,
        T(user_id, 'alert_step1'),
        parse_mode='HTML',
        reply_markup=kb
    )
    register_panel_owner(message.chat.id, msg.message_id, user_id)


def _finalize_alert(message, user_id, crypto_id, direction_raw, price_raw):
    chat_id = message.chat.id
    try:
        target = float(str(price_raw).replace(',', ''))
    except ValueError:
        bot.send_message(chat_id, T(user_id, 'alert_invalid_price'), parse_mode='HTML')
        return

    current = get_crypto_price(crypto_id)
    if not current:
        bot.send_message(chat_id, T(user_id, 'alert_fetch_fail'))
        return

    if direction_raw == 'cross':
        direction = 'above' if target > current else 'below'
    elif direction_raw in ('above', 'over', 'up', '📈'):
        direction = 'above'
    elif direction_raw in ('below', 'under', 'down', '📉'):
        direction = 'below'
    else:
        bot.send_message(chat_id, T(user_id, 'alert_bad_direction'), parse_mode='HTML')
        return

    existing = db_get_alerts(user_id)
    if len(existing) >= MAX_ALERTS_PER_USER:
        bot.send_message(chat_id, T(user_id, 'alert_limit_reached', max=MAX_ALERTS_PER_USER))
        return

    for a in existing:
        if a['crypto_id'] == crypto_id and a['target_price'] == target and a['direction'] == direction:
            bot.send_message(chat_id, T(user_id, 'alert_duplicate'))
            return

    symbol = _sym(crypto_id)
    db_add_alert(user_id, crypto_id, symbol, target, direction)
    arrow    = '📈' if direction == 'above' else '📉'
    dword    = T(user_id, 'above_word') if direction == 'above' else T(user_id, 'below_word')
    diff     = abs(target - current)
    diff_pct = (diff / current) * 100 if current else 0.0

    kb = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(T(user_id, 'btn_my_alerts'),       callback_data="show_alerts"),
        types.InlineKeyboardButton(T(user_id, 'btn_add_another_alert'), callback_data="alrt_new"),
    ]])
    bot.send_message(
        chat_id,
        T(user_id, 'alert_set',
          sym=symbol, arrow=arrow, direction=dword, target=fmt_price(target),
          current=fmt_price(current), diff=fmt_price(diff),
          pct=f"{diff_pct:.1f}", count=len(existing)+1, max=MAX_ALERTS_PER_USER),
        parse_mode='HTML',
        reply_markup=kb
    )
    logger.info(f"User {user_id} set alert: {symbol} {direction}")


@bot.message_handler(commands=['alerts'])
@rate_limit_check
@loading_indicator
def list_alerts(message):
    user_id = message.from_user.id
    alerts = db_get_alerts(user_id)
    if not alerts:
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_set_alert'), callback_data="alrt_new")
        ]])
        bot.reply_to(message, T(user_id, 'no_alerts'), parse_mode='HTML', reply_markup=kb)
        return

    keyboard = []
    above_w = T(user_id, 'above_word')
    below_w = T(user_id, 'below_word')
    header = T(user_id, 'alerts_header', count=len(alerts), max=MAX_ALERTS_PER_USER)
    body = []
    # Batch-fetch all alert prices at once
    alert_ids = [a['crypto_id'] for a in alerts if a['crypto_id'] in CRYPTO_LIST]
    if alert_ids:
        _fetch_prices_batch(','.join(set(alert_ids)))
    for a in alerts:
        arrow  = '📈' if a['direction'] == 'above' else '📉'
        cur    = get_crypto_price(a['crypto_id'])
        dword  = above_w if a['direction'] == 'above' else below_w
        if cur:
            pct_str = f"{abs((a['target_price']-cur)/cur)*100:.1f}"
            dist = f"  <i>{T(user_id, 'away_pct', pct=pct_str)}</i>"
        else:
            dist = ""
        body.append(f"{arrow} <b>{a['symbol']}</b> {dword} <b>{fmt_price(a['target_price'])}</b>{dist}")
        keyboard.append([types.InlineKeyboardButton(
            f"🗑  {a['symbol']} {dword} {fmt_price(a['target_price'])}",
            callback_data=f"alertdel_{a['id']}"
        )])
    text = header + (quote("\n".join(body)) if body else "")
    keyboard.append([
        types.InlineKeyboardButton(T(user_id, 'btn_add_alert'),  callback_data="alrt_new"),
        types.InlineKeyboardButton(T(user_id, 'btn_delete_all'), callback_data="alertdelall"),
    ])
    bot.reply_to(
        message,
        text,
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(keyboard)
    )


@bot.message_handler(commands=['compare'])
@rate_limit_check
@loading_indicator
def compare_cmd(message):
    # Always show the coin picker (buttons)
    kb = _coin_picker_keyboard("cmp1_")

    msg = bot.reply_to(
        message,
        T(message.from_user.id, 'compare_pick1'),
        parse_mode='HTML',
        reply_markup=kb
    )
    # Register panel owner
    register_panel_owner(message.chat.id, msg.message_id, message.from_user.id)


def _coin_picker_keyboard(prefix: str, coins=None, cancel=None,
                          user_id: int = 0) -> types.InlineKeyboardMarkup:
    """Standard coin-picker grid: 3 buttons per row, each button labelled with
    the coin symbol and firing callback ``prefix + <coin_id>``.

    ``coins``   — optional coin-id list (defaults to CRYPTO_LIST minus
                 telegram-stars)
    ``cancel``  — optional callback_data for a trailing cancel row, labelled
                 with T(user_id, 'btn_cancel')
    """
    if coins is None:
        coins = [c for c in CRYPTO_LIST.keys() if c != 'telegram-stars']
    rows = []
    for i in range(0, len(coins), 3):
        rows.append([
            types.InlineKeyboardButton(_sym(cid), callback_data=f"{prefix}{cid}")
            for cid in coins[i:i + 3]
        ])
    if cancel:
        rows.append([types.InlineKeyboardButton(T(user_id, 'btn_cancel'),
                                                callback_data=cancel)])
    return types.InlineKeyboardMarkup(rows)


def _do_compare(message, raw1, raw2, user_id: int = 0, edit_msg_id=None):
    """
    Called from both /compare command and callback buttons.
    Uses send_message (not reply_to) so it works even when the
    original picker message has been deleted.
    Uses get_crypto_price() (cached) to avoid CoinGecko 429 errors.
    """
    chat_id = message.chat.id
    # Show loading message for compare operation
    cmp_uid_pre = user_id or getattr(getattr(message, "from_user", None), "id", 0)
    loading_msg = None
    try:
        loading_msg = bot.send_message(
            chat_id,
            add_timestamp(T(cmp_uid_pre, 'loading_compare')),
            parse_mode='HTML'
        )
    except Exception:
        pass
    
    bot.send_chat_action(chat_id, 'typing')

    ids, names = [], []
    for raw in (raw1, raw2):
        cid = detect_currency(raw.lower())
        if not cid:
            cid = raw.lower()
        if cid not in CRYPTO_LIST:
            uid_tmp = user_id or getattr(getattr(message, "from_user", None), "id", 0)
            bot.send_message(
                chat_id,
                T(uid_tmp, 'unknown_coin', sym=html.escape(raw)),
                parse_mode='HTML'
            )
            if loading_msg:
                try:
                    bot.delete_message(loading_msg.chat.id, loading_msg.message_id)
                except Exception:
                    pass
            return
        ids.append(cid)
        names.append(_sym(cid))

    usd_to_irr = get_usd_to_irr()
    cmp_uid = user_id or getattr(getattr(message, "from_user", None), "id", 0)
    
    # Multi-source batch fetch (rate-limited)
    batch = _fetch_prices_batch(','.join(ids))
    
    # CoinGecko extra data (market cap, volume) - rate-limited
    extra = {}
    try:
        ids_str = ','.join(ids)
        if ids_str:
            @rate_limited_api_call
            def _fetch_extra():
                return session.get(
                    "https://api.coingecko.com/api/v3/simple/price"
                    f"?ids={ids_str}&vs_currencies=usd"
                    f"&include_market_cap=true&include_24hr_vol=true",
                    timeout=8
                )
            r = _fetch_extra()
            if r and r.status_code == 200:
                extra = r.json()
    except Exception:
        pass

    blocks = []
    prices = []
    changes = []
    for cid, name in zip(ids, names):
        bp = batch.get(cid, {})
        price = bp.get('usd', 0)
        prices.append(price)
        change = bp.get('usd_24h_change')
        cd = extra.get(cid, {})
        mcap = cd.get('usd_market_cap', 0)
        vol = cd.get('usd_24h_vol', 0)
        changes.append(change or 0)
        # Format change in English always (not Persian digits)
        chg = _chg_str(change) if change is not None else ""
        vol_str  = f"${vol/1e6:,.1f}M"  if vol  else "-"
        mcap_str = f"${mcap/1e9:,.2f}B" if mcap else "-"
        
        # Format IRR price in user's locale (may be Persian)
        irr_formatted = _fmt_number(f"{price * usd_to_irr:,.0f}", cmp_uid) if usd_to_irr else None
        irr_row = f"💰 {irr_formatted} {T(cmp_uid, 'toman_label')}\n" if irr_formatted else ""
        
        blocks.append(
            f"🪙 <b>{name}</b> \u2002{fmt_price(price)}  {chg}\n"
            + irr_row
            + T(cmp_uid, 'compare_vol', vol=vol_str)
            + T(cmp_uid, 'compare_mcap', mcap=mcap_str)
        )

    if changes[0] == changes[1]:
        verdict = T(cmp_uid, 'compare_tied')
    else:
        winner  = names[0] if changes[0] > changes[1] else names[1]
        verdict = T(cmp_uid, 'compare_winner', name=winner)

    kb = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(
            T(cmp_uid, 'btn_refresh'), 
            callback_data=f"cmpref_{ids[0]}_{ids[1]}"
        )
    ]])
    # Apply blockquote formatting to comparison blocks
    text = add_timestamp(T(cmp_uid, 'compare_header') + quote("\n\n".join(blocks)) + f"\n\n{verdict}")

    if edit_msg_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=edit_msg_id, parse_mode='HTML', reply_markup=kb)
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=kb)
    else:
        msg = bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=kb)
        register_panel_owner(chat_id, msg.message_id, cmp_uid)
    
    # Clean up loading message
    if loading_msg:
        try:
            bot.delete_message(loading_msg.chat.id, loading_msg.message_id)
        except Exception:
            pass


@bot.message_handler(commands=['market'])
@rate_limit_check
def market_cmd(message, user_id=None, edit_msg_id=None):
    uid_m = user_id or message.from_user.id
    # Show loading message for market data
    loading_msg = None
    try:
        loading_msg = bot.send_message(
            message.chat.id,
            add_timestamp(T(uid_m, 'loading_market')),
            parse_mode='HTML'
        )
    except Exception:
        pass
    
    bot.send_chat_action(message.chat.id, 'typing')
    # Try fresh data first, fall back to any cached (up to 1h stale)
    g = cache_get('market_global_data')
    if not g:
        for attempt in range(2):
            try:
                resp = session.get("https://api.coingecko.com/api/v3/global", timeout=8)
                if resp.status_code == 200:
                    g = resp.json().get('data', {})
                    if g:
                        cache_set('market_global_data', g, ttl=300)
                        cache_set('market_global_data_stale', g, ttl=3600)
                    break
                elif resp.status_code == 429:
                    cache_set('cg_rate_limited', True, ttl=60)
                    time.sleep(2)
            except Exception:
                break
    if not g:
        g = cache_get('market_global_data_stale')
    if not g:
        bot.send_message(message.chat.id, T(uid_m, 'market_unavailable'))
        # Don't leave the stale "Fetching market data…" bubble on screen
        if loading_msg:
            try:
                bot.delete_message(loading_msg.chat.id, loading_msg.message_id)
            except Exception:
                pass
        return

    try:
        fg_resp = session.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        _fg_list = fg_resp.json().get('data') or [{}]
        fg_data = _fg_list[0] if _fg_list else {}
        fg_str = _fg_block(uid_m, fg_data)
    except Exception:
        fg_str = T(uid_m, 'fg_unavailable')

    mcap    = g.get('total_market_cap', {}).get('usd', 0)
    vol     = g.get('total_volume', {}).get('usd', 0)
    btc_dom = g.get('market_cap_percentage', {}).get('btc', 0)
    eth_dom = g.get('market_cap_percentage', {}).get('eth', 0)
    coins   = g.get('active_cryptocurrencies', 0)
    chg24   = g.get('market_cap_change_percentage_24h_usd', 0)
    arrow   = '📈' if chg24 >= 0 else '📉'

    kb = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(T(uid_m, 'btn_refresh'), callback_data="market_refresh")
    ]])
    text = add_timestamp(
        T(uid_m, 'market_header') +
        quote(
            T(uid_m, 'market_mcap', mcap=f"${mcap/1e12:.2f}T", arrow=arrow, chg=f"{chg24:+.1f}") +
            T(uid_m, 'market_vol',  vol=f"${vol/1e9:.1f}B") +
            T(uid_m, 'market_dom',  btc=f"{btc_dom:.1f}", eth=f"{eth_dom:.1f}") +
            T(uid_m, 'market_coins', coins=f"{coins:,}") +
            T(uid_m, 'market_fg', fg=fg_str)
        )
    )

    if edit_msg_id:
        try:
            bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=edit_msg_id,
                parse_mode='HTML',
                reply_markup=kb
            )
        except Exception as e:
            err_str = str(e)
            if "message is not modified" not in err_str.lower():
                bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=kb)
    else:
        bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=kb)
    
    # Clean up loading message
    if loading_msg:
        try:
            bot.delete_message(loading_msg.chat.id, loading_msg.message_id)
        except Exception:
            pass


def _build_digest_keyboard(enabled, hour, user_id=0):
    """Build digest keyboard with checkmark on the currently selected hour."""
    preset_hours = [7, 9, 12, 18, 21]
    time_row = []
    for h in preset_hours:
        label = f"✅ {h:02d}:00" if h == hour else f"{h:02d}:00"
        time_row.append(types.InlineKeyboardButton(label, callback_data=f"digest_h{h}"))
    return types.InlineKeyboardMarkup([
        [
            types.InlineKeyboardButton(T(user_id, "digest_enabled") if enabled else T(user_id, "btn_enable"), callback_data="digest_on"),
            types.InlineKeyboardButton(T(user_id, "btn_disable") if enabled else T(user_id, "digest_disabled"), callback_data="digest_off"),
        ],
        time_row,
        [types.InlineKeyboardButton(T(user_id, "btn_custom_time"), callback_data="digest_custom")],
    ])


@bot.message_handler(commands=['digest'])
@rate_limit_check
@loading_indicator
def digest_cmd(message):
    user_id = message.from_user.id
    pref    = db_get_digest(user_id)
    enabled = pref['enabled'] if pref else False
    hour    = pref['hour']    if pref else 9
    uid_d   = message.from_user.id
    status  = T(uid_d, 'digest_enabled') if enabled else T(uid_d, 'digest_disabled')
    bot.reply_to(
        message,
        T(uid_d, 'digest_header') + T(uid_d, 'digest_status', status=status, hour=f"{hour:02d}"),
        parse_mode='HTML',
        reply_markup=_build_digest_keyboard(enabled, hour, uid_d)
    )


# ─────────────────────────────────────────────
# Admin Commands (Owner Only)
# ─────────────────────────────────────────────

def is_owner(user_id: int) -> bool:
    """Check if user is the bot owner."""
    return OWNER_USER_ID and user_id == OWNER_USER_ID

@bot.message_handler(commands=['test'])
@loading_indicator
def run_tests(message):
    """Owner-only diagnostic: test all features and return a combined report."""
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "⛔ Owner only command.")
        return

    chat_id = message.chat.id
    bot.send_chat_action(chat_id, 'typing')

    results = []
    failed = 0

    def ok(text):
        results.append(f"  ✅ {text}")

    def fail(text, detail=""):
        nonlocal failed
        failed += 1
        d = f" - {html.escape(str(detail))}" if detail else ""
        results.append(f"  ❌ {text}{d}")

    def section(title):
        results.append(f"\n━━━ {title} ━━━")

    # ── Database ──────────────────────────────────────────
    section("DATABASE")
    try:
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM user_languages")
        ok("SQLite connection and user_languages table")
    except Exception as e:
        fail("SQLite", str(e))

    # ── Imports & constants ──────────────────────────────
    section("IMPORTS & CONSTANTS")
    try:
        from number_utils import parse_number, format_crypto, format_fiat
        ok("number_utils imports")
    except Exception as e:
        fail("number_utils import", str(e))

    ok(f"OWNER_USER_ID = {OWNER_USER_ID}" if OWNER_USER_ID else "OWNER_USER_ID not set")
    ok(f"{len(CRYPTO_LIST)} coins in CRYPTO_LIST")
    ok(f"{len(CRYPTO_ALIASES)} aliases in CRYPTO_ALIASES")
    ok(f"MAX_ALERTS_PER_USER = {MAX_ALERTS_PER_USER}")
    ok(f"MAX_WALLETS_PER_USER = {MAX_WALLETS_PER_USER}")

    # ── Number parsing ──────────────────────────────────
    section("NUMBER PARSING")
    test_cases = [
        ("1.000.000",      "European millions"),
        ("1,000,000",      "US millions"),
        ("1.234,56",       "European decimal"),
        ("1,234.56",       "US decimal"),
        ("۱۲۳٬۴۵۶",       "Persian"),
        ("0.00012300",     "Small with trailing zeros"),
    ]
    for raw, label in test_cases:
        try:
            r = parse_number(raw)
            if r is not None:
                ok(f"parse_number({raw!r}) = {r}  ({label})")
            else:
                fail(f"parse_number({raw!r}) returned None  ({label})")
        except Exception as e:
            fail(f"parse_number({raw!r})  ({label})", str(e))

    try:
        f = format_crypto(Decimal("0.00012300"))
        assert f == "0.000123", f"Expected '0.000123', got {f!r}"
        ok(f"format_crypto(0.00012300) = {f!r}")
    except Exception as e:
        fail("format_crypto", str(e))

    # ── Address validation ──────────────────────────────
    section("ADDRESS VALIDATION")
    ok(f"TRON length+prefix: {is_valid_tron_address('T' + 'a'*33)}")
    ok(f"TRON short rejected: {not is_valid_tron_address('TMhVB8xvL8rQ9p')}")
    ok(f"TRON bad prefix rejected: {not is_valid_tron_address('A' + 'a'*33)}")
    ok(f"TON valid(EQ...): {is_valid_ton_address('EQD4v3pGbRfL8yYjKQVw5wZxgHJXqXxGq7UqGcXvZ3xJh5aN')}")
    ok(f"TON invalid(short): {not is_valid_ton_address('EQD4v3')}")

    # ── detect_currency ─────────────────────────────────
    section("CURRENCY DETECTION")
    for alias, expected in [("btc", "bitcoin"), ("بیتکوین", "bitcoin"), ("تومان", "toman"), ("دلار", "usd")]:
        try:
            r = detect_currency(alias)
            status = ok if r == expected else fail
            status(f"detect_currency({alias!r}) = {r!r}")
        except Exception as e:
            fail(f"detect_currency({alias!r})", str(e))

    # ── evaluate_math ──────────────────────────────────
    section("MATH EVALUATION")
    for expr, expected in [("2+2", "4"), ("5*3", "15"), ("10/2", "5"), ("2**8", "256")]:
        try:
            r = evaluate_math(expr)
            if isinstance(r, str) and f"= {expected}" in r:
                ok(f"evaluate_math({expr!r}) -> {r}")
            else:
                fail(f"evaluate_math({expr!r}) = {r!r}, expected '= {expected}' in result")
        except Exception as e:
            fail(f"evaluate_math({expr!r})", str(e))

    # ── API connectivity ───────────────────────────────
    section("API CONNECTIVITY")

    # CoinGecko direct
    try:
        resp = session.get("https://api.coingecko.com/api/v3/ping", timeout=10)
        ok(f"CoinGecko ping: HTTP {resp.status_code}")
    except Exception as e:
        fail("CoinGecko ping", str(e))

    # Single price fetch via batch
    try:
        price_data = _fetch_prices_batch("bitcoin")
        if price_data and "bitcoin" in price_data:
            btc_price = price_data["bitcoin"].get("usd", 0)
            ok(f"BTC price: ${btc_price}")
        else:
            fail("Fetch BTC price - empty response")
    except Exception as e:
        fail("Fetch BTC price", str(e))

    # USD to IRR
    try:
        rate = get_usd_to_irr()
        ok(f"USD/IRR rate: {rate:,}")
    except Exception as e:
        fail("USD/IRR rate", str(e))

    # CryptoCompare fallback
    try:
        resp = session.get("https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD", timeout=10)
        ok(f"CryptoCompare: HTTP {resp.status_code}")
    except Exception as e:
        fail("CryptoCompare", str(e))

    # Gold prices
    try:
        gold = get_gold_prices()
        ok(f"Gold data received ({len(gold)} keys)")
    except Exception as e:
        fail("Gold prices", str(e))

    # ── All crypto prices (batch) ──────────────────────
    section("ALL CRYPTO PRICES")
    all_ids = ",".join(k for k in CRYPTO_LIST if k != "telegram-stars")
    try:
        all_prices = _fetch_prices_batch(all_ids)
        if all_prices:
            ok(f"Batch price fetch returned {len(all_prices)} coins")
            for cid in CRYPTO_LIST:
                if cid in all_prices:
                    p = all_prices[cid].get("usd", 0)
                    chg = all_prices[cid].get("usd_24h_change")
                    chg_str = f" ({chg:+.2f}%)" if chg is not None else ""
                    ok(f"  {_sym(cid)}: ${p}{chg_str}")
                elif cid == "telegram-stars":
                    ok(f"  STARS: $0.015 (official rate)")
                else:
                    fail(f"  {_sym(cid)} - not in response")
        else:
            fail("Batch price fetch returned empty")
    except Exception as e:
        fail("Batch price fetch", str(e))

    # ── Convert amount ─────────────────────────────────
    section("CONVERSION")
    for amt, src, dst, label in [
        (1, "usd", "toman", "USD → Toman"),
        (100_000, "toman", "usd", "Toman → USD"),
        (1, "bitcoin", "usd", "BTC → USD"),
        (1, "ethereum", "toman", "ETH → Toman"),
    ]:
        try:
            r, err = convert_amount(float(amt), src, dst)
            if err:
                fail(f"{label}: {err}")
            else:
                ok(f"{label}: {amt} {src} = {r:.4f} {dst}")
        except Exception as e:
            fail(label, str(e))

    # ── Cache ──────────────────────────────────────────
    section("CACHE")
    try:
        cache_set("_test_key", "ok")
        v = cache_get("_test_key")
        ok(f"Cache set/get: {v!r}")
    except Exception as e:
        fail("Cache", str(e))

    # ── Timestamp ──────────────────────────────────────
    section("TIMESTAMP")
    ts = add_timestamp("test")
    ok(f"add_timestamp() produces time (length {len(ts)})")

    # ── Summary ──────────────────────────────────────
    section("SUMMARY")
    total = len([l for l in results if l.startswith("  ✅") or l.startswith("  ❌")])
    passed = total - failed
    pct = (passed / total * 100) if total else 0
    results.append(f"\n  {passed}/{total} tests passed ({pct:.0f}%)")
    if failed == 0:
        results.append("\n  🎉 All tests passed!")
    else:
        results.append(f"\n  ⚠️  {failed} test(s) failed - check the ❌ entries above.")

    report = "🔬 <b>Bot Diagnostic Report</b>\n" + "\n".join(results)

    # Split long messages (Telegram 4096 limit).  Fixed-width HTML chunks can
    # split a tag mid-way and Telegram then rejects the whole message with a
    # 400, so fall back to plain text for that chunk on parse errors.
    for chunk in [report[i:i+4000] for i in range(0, len(report), 4000)]:
        try:
            bot.send_message(chat_id, chunk, parse_mode='HTML')
        except Exception:
            bot.send_message(chat_id, _strip_html_tags(chunk))

@bot.message_handler(commands=['admin'])
@rate_limit_check
@loading_indicator
def admin_panel(message):
    global _cache
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "⛔ Owner only command.")
        return
    
    # Get bot statistics
    with get_db_conn() as conn:
        c = conn.cursor()
        
        # Count total users
        c.execute("SELECT COUNT(DISTINCT user_id) FROM user_languages")
        total_users = c.fetchone()[0]
        
        # Count active alerts
        c.execute("SELECT COUNT(*) FROM alerts")
        total_alerts = c.fetchone()[0]
        
        # Count holdings
        c.execute("SELECT COUNT(*) FROM holdings")
        total_holdings = c.fetchone()[0]
        
        # Count wallets
        c.execute("SELECT COUNT(*) FROM wallets")
        total_wallets = c.fetchone()[0]
        
    
    # Get cache stats
    cache_size = len(_cache)
    
    kb = types.InlineKeyboardMarkup([
        [types.InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast")],
        [types.InlineKeyboardButton("🔄 Clear Cache", callback_data="admin_clear_cache")],
        [types.InlineKeyboardButton("📊 Detailed Stats", callback_data="admin_stats")]
    ])
    
    msg = (
        f"🔧 <b>Admin Panel</b>\n"
        + quote(
            f"👥 Total Users: <b>{total_users}</b>\n"
            f"🔔 Active Alerts: <b>{total_alerts}</b>\n"
            f"💼 Holdings: <b>{total_holdings}</b>\n"
            f"👛 Tracked Wallets: <b>{total_wallets}</b>\n"
            f"🗂️ Cache Size: <b>{cache_size}</b> items"
        )
        + "\n<i>Select an action below:</i>"
    )
    
    sent_msg = bot.reply_to(message, msg, parse_mode='HTML', reply_markup=kb)
    register_panel_owner(message.chat.id, sent_msg.message_id, message.from_user.id)

# ─────────────────────────────────────────────
# General text handler  ← catch-all, must stay LAST
# ─────────────────────────────────────────────
def _handle_text_state(message, user_id, text, text_lower, text_original, state):
    # ── State machine ─────────────────────────

    # Suggestion forwarding (must be before admin broadcast)
    if state == 'awaiting_suggestion':
        del_user_state(user_id)
        if SUGGESTION_CHAT_ID:
            try:
                forward_text = (
                    f"💡 <b>New Suggestion</b>\n"
                    + quote(
                        f"👤 User: {message.from_user.id}\n"
                        f"👤 Name: {html.escape(message.from_user.full_name or message.from_user.first_name or '')}\n"
                        f"💬 {html.escape(message.text)}"
                    )
                )
                bot.send_message(SUGGESTION_CHAT_ID, forward_text, parse_mode='HTML')
                bot.reply_to(message, T(user_id, 'suggest_sent'))
            except Exception as e:
                logger.error(f"Failed to forward suggestion: {e}")
                bot.reply_to(message, T(user_id, 'suggest_fail'))
        else:
            bot.reply_to(message, T(user_id, 'suggest_fail'))
        return True

    # Admin broadcast
    if state == 'admin_broadcast':
        if not is_owner(user_id):
            return True
        
        del_user_state(user_id)
        
        # Get all users
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT DISTINCT user_id FROM user_languages")
            all_users = [row[0] for row in c.fetchall()]
        
        from telebot.apihelper import ApiTelegramException

        broadcast_msg = message.text

        bot.reply_to(message, f"📢 Broadcasting to {len(all_users)} users...")

        # Run in a dedicated thread: the sequential per-user loop takes many
        # minutes for large user bases and used to stall the entire polling
        # thread (every other user request queued behind it).
        def _broadcast_worker():
            sent_count = 0
            failed_count = 0
            for target_user in all_users:
                for attempt in range(3):
                    try:
                        bot.send_message(target_user, broadcast_msg, parse_mode='HTML')
                        sent_count += 1
                        time.sleep(0.033)
                        break
                    except ApiTelegramException as e:
                        if 'retry after' in str(e).lower():
                            wait = re.search(r'(\d+)', str(e))
                            wait_sec = int(wait.group(1)) + 1 if wait else 5
                            logger.warning(f"Flood wait {wait_sec}s for user {target_user}")
                            time.sleep(wait_sec)
                            continue
                        failed_count += 1
                        logger.error(f"Broadcast failed for user {target_user}: {e}")
                        break
                    except Exception as e:
                        failed_count += 1
                        logger.error(f"Broadcast failed for user {target_user}: {e}")
                        break

            bot.send_message(
                message.chat.id,
                f"✅ <b>Broadcast Complete</b>\n"
                + quote(f"✅ Sent: <b>{sent_count}</b>\n❌ Failed: <b>{failed_count}</b>"),
                parse_mode='HTML'
            )

        threading.Thread(target=_broadcast_worker, daemon=True, name='BroadcastWorker').start()
        return True

    # Adding a wallet via the inline ➕ button
    if state == 'add_wallet_inline':
        del_user_state(user_id)
        _process_add_wallet(message, user_id, text.strip())
        return True

    # Digest custom time input
    if state == 'digest_custom_hour':
        del_user_state(user_id)
        try:
            hour = int(text.strip())
            if not 0 <= hour <= 23:
                raise ValueError
        except ValueError:
            bot.reply_to(message, T(user_id, 'invalid_hour'))
            return True
        # Default to 9 AM Iran time
        pref = db_get_digest(user_id) or {'enabled': False, 'hour': 9}
        db_set_digest(user_id, pref['enabled'], hour)
        bot.reply_to(
            message,
            T(user_id, 'digest_time_confirm', hour=f"{hour:02d}"),
            parse_mode='HTML',
            reply_markup=_build_digest_keyboard(pref['enabled'], hour, user_id)
        )
        return True

    # Convert wizard step 3 - waiting for amount
    if state and state.startswith('convert_'):
        del_user_state(user_id)
        _, from_cid, to_cid = state.split('_', 2)
        from_sym = _sym(from_cid)
        to_sym   = _sym(to_cid)
        try:
            amount = float(text.strip().replace(',', ''))
        except ValueError:
            bot.reply_to(message, T(user_id, 'invalid_amount_conv'), parse_mode='HTML')
            return True
        bot.send_chat_action(message.chat.id, 'typing')
        result_val, err = convert_amount(amount, from_cid, to_cid)
        if err:
            bot.reply_to(message, T(user_id, 'convert_fail', err=err))
        else:
            usd_to_irr = get_usd_to_irr()
            if to_cid == 'usd':
                result_str_w = fmt_price(result_val)
                display_to_sym_w = ""
                toman_note = T(user_id, 'convert_toman_note', irr=f"{result_val * usd_to_irr:,.0f}") if usd_to_irr else ""
            elif to_cid == 'toman':
                result_str_w = f"{result_val:,.0f}"
                display_to_sym_w = T(user_id, 'toman_label')
                toman_note = ""
            else:
                result_str_w = f"{result_val:,.6g}"
                display_to_sym_w = to_sym
                usd_val = result_val * (get_crypto_price(to_cid) or 0)
                toman_note = T(user_id, 'convert_toman_approx', usd=fmt_price(usd_val), irr=f"{usd_val * usd_to_irr:,.0f}") if usd_to_irr and usd_val else ""
            # Header outside the quote; the toman note gets its own quote block
            msg = T(user_id, 'convert_result', amount=f"{amount:,g}", from_sym=from_sym,
                    result=result_str_w, to_sym=display_to_sym_w)
            if toman_note:
                msg += quote(toman_note)
            bot.reply_to(
                message,
                add_timestamp(msg),
                parse_mode='HTML'
            )
        return True

    # Stars amount calculator - waiting for the number of stars
    if state == 'stars_amount':
        try:
            amount = float(text.strip().replace(',', ''))
            if amount <= 0:
                raise ValueError("non-positive")
        except ValueError:
            bot.reply_to(message, T(user_id, 'stars_invalid'), parse_mode='HTML')
            return True  # keep state so user can retry
        del_user_state(user_id)
        bot.send_chat_action(message.chat.id, 'typing')
        stars_price = get_crypto_price('telegram-stars')
        if not stars_price:
            stars_price = float(os.getenv('STARS_PRICE_USD', '0.015'))
        msg = _stars_price_lines(user_id, stars_price, amount)
        bot.reply_to(message, add_timestamp(msg), parse_mode='HTML')
        return True

    # Alert wizard step 3 - waiting for target price
    if state and state.startswith('alert_price_'):
        del_user_state(user_id)
        parts_s = state.split('_', 3)
        if len(parts_s) != 4:
            bot.reply_to(message, T(user_id, 'alert_state_error'))
            return True
        _, _, cid, direction = parts_s
        _finalize_alert(message, user_id, cid, direction, text.strip())
        return True

    # Adding a holding via the inline ➕ button
    # Coin picker amount step (from /set, hadd, hpick buttons)
    if state and state.startswith('hpick_amount_'):
        cid = state[len('hpick_amount_'):]
        sym = _sym(cid)
        try:
            amount = float(text.strip().replace(',', ''))
            if amount < 0:
                raise ValueError("Negative")
        except ValueError:
            bot.reply_to(message, T(user_id, 'invalid_amount'), parse_mode='HTML')
            return True  # keep state so user can retry
        del_user_state(user_id)
        saved = db_get_holdings(user_id) or {}
        # FIX: Overwrite instead of add for /set command
        saved[sym] = amount
        db_set_holdings(user_id, saved)
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        inner_kb = build_holdings_keyboard(saved, user_id)
        bot.reply_to(
            message,
            T(user_id, 'holding_set', sym=sym, amount=f"{amount:,g}", total=f"{amount:,g}") +
            holdings_message_text(saved, usd_to_irr, buy_prices, user_id),
            parse_mode='HTML',
            reply_markup=inner_kb
        )
        logger.info(f"User {user_id} set holding: {sym} = {amount}")
        return True

    # Setting buy price for P&L
    if state and state.startswith('set_buy_price_'):
        symbol = state[len('set_buy_price_'):]
        try:
            buy_price = float(text.strip().replace(',', ''))
            if buy_price <= 0:
                raise ValueError("Price must be positive")
        except ValueError:
            bot.reply_to(message, T(user_id, 'invalid_price'))
            return True
        db_set_buy_price(user_id, symbol, buy_price)
        del_user_state(user_id)
        saved = db_get_holdings(user_id) or {}
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        bot.reply_to(
            message,
            holdings_message_text(saved, usd_to_irr, buy_prices, user_id),
            parse_mode='HTML',
            reply_markup=build_holdings_keyboard(saved, user_id)
        )
        return True

    # Editing an existing holding amount
    if state and state.startswith('edit_holding_'):
        symbol = state[len('edit_holding_'):]
        try:
            new_amount = float(text.strip().replace(',', ''))
            if new_amount < 0:
                raise ValueError
        except ValueError:
            bot.reply_to(message, T(user_id, 'invalid_amount'))
            return True  # keep state so user can retry
        saved = db_get_holdings(user_id) or {}
        saved[symbol] = new_amount
        db_set_holdings(user_id, saved)
        del_user_state(user_id)
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        bot.reply_to(
            message,
            holdings_message_text(saved, usd_to_irr, buy_prices, user_id),
            parse_mode='HTML',
            reply_markup=build_holdings_keyboard(saved, user_id)
        )
        return True

    return False

def _handle_text_math(message, user_id, text, text_lower, text_original):
    # Math expressions - accepts ASCII and Persian-Indic digits/operators (normalized above)
    _has_pct_of = ('% of' in text_lower or '%of' in text_lower
                or '% از' in text_lower or '%از' in text_lower
                or '٪ از' in text_original or '٪از' in text_original)

    # ⚠️ FIX: Validate before attempting math evaluation
    # Must contain digits AND operators, but not ONLY operators
    has_digit = bool(re.search(r'\d', text))
    has_operator = bool(re.search(r'[\+\-\*\/\%\^]', text))
    is_only_operator = bool(re.match(r'^[\+\-\*\/\%\^\(\)\.\s]+$', text))

    if (re.match(r'^[\d+\-*/().%\s^]+$', text) and len(text) > 1
            and has_digit and has_operator and not is_only_operator) \
            or _has_pct_of:
        result = evaluate_math(text_original, user_id)
        bot.reply_to(message, result)
        return True

    return False

def _handle_text_fiat(message, user_id, text_lower):
    is_group = message.chat.type in ('group', 'supergroup')
    chat_id = message.chat.id
    # USD → Toman (support flexible number formats)
    usd_pattern = r'^([\d.,۰-۹٬٫]+)?\s*(\$|usd|dollar|دلار)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$'
    m = re.match(usd_pattern, text_lower)
    if m:
        amount_str = m.group(1)
        amount = parse_number(amount_str) if amount_str else Decimal('1')
        if amount is None:
            amount = Decimal('1')
        
        usd_to_irr = get_usd_to_irr()
        if usd_to_irr is None:
            bot.reply_to(message, T(user_id, 'rate_unavailable'), parse_mode='HTML')
            return True
        result_toman = amount * Decimal(str(usd_to_irr))
        
        # Format with proper number handling
        user_lang = db_get_lang(user_id)
        amount_formatted = format_fiat(amount)
        result_formatted = format_fiat(result_toman, decimals=0)
        
        # Apply locale
        amount_formatted = format_for_locale(amount_formatted, user_lang)
        result_formatted = format_for_locale(result_formatted, user_lang)
        
        reply_text = T(user_id, 'usd_conversion', amount=amount_formatted, result=result_formatted)
        
        bot.reply_to(message, add_timestamp(reply_text), parse_mode='HTML')
        if is_group:
            query_key = f"fiat:usd_to_toman:{text_lower}"
            raw_data = {'rate': usd_to_irr, 'amount': str(amount)}
            set_group_cached_query(chat_id, query_key, raw_data)
        return True

    # TRY → Toman (support flexible number formats)
    try_pattern = r'^([\d.,۰-۹٬٫]+)?\s*(lira|tl|try|₺)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$'
    m = re.match(try_pattern, text_lower)
    if m:
        amount_str = m.group(1)
        amount = parse_number(amount_str) if amount_str else Decimal('1')
        if amount is None:
            amount = Decimal('1')

        try_to_irr = get_try_to_irr()
        if try_to_irr is None:
            bot.reply_to(message, add_timestamp(T(user_id, 'rate_unavailable')), parse_mode='HTML')
            return True
        result_toman = amount * Decimal(str(try_to_irr))

        user_lang = db_get_lang(user_id)
        amount_formatted = format_fiat(amount)
        result_formatted = format_fiat(result_toman, decimals=0)
        amount_formatted = format_for_locale(amount_formatted, user_lang)
        result_formatted = format_for_locale(result_formatted, user_lang)

        reply_text = T(user_id, 'try_conversion', amount=amount_formatted, result=result_formatted)

        bot.reply_to(message, add_timestamp(reply_text), parse_mode='HTML')
        if is_group:
            query_key = f"fiat:try_to_toman:{text_lower}"
            raw_data = {'rate': try_to_irr, 'amount': str(amount)}
            set_group_cached_query(chat_id, query_key, raw_data)
        return True

    # EUR / GBP / AED / CNY → Toman (support flexible number formats)
    fx_defs = [
        (r'^([\d.,۰-۹٬٫]+)?\s*(eur|euro|€)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$', get_eur_to_irr, 'EUR', '🇪🇺'),
        (r'^([\d.,۰-۹٬٫]+)?\s*(gbp|pound|£)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$', get_gbp_to_irr, 'GBP', '🇬🇧'),
        (r'^([\d.,۰-۹٬٫]+)?\s*(aed|dirham|درهم)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$', get_aed_to_irr, 'AED', '🇦🇪'),
        (r'^([\d.,۰-۹٬٫]+)?\s*(cny|yuan|yuen|یوآن)\s*(?:to|به)?\s*(toman|تومان|تومن|irr|ریال)?$', get_cny_to_irr, 'CNY', '🇨🇳'),
    ]
    for fx_pattern, fx_getter, fx_code, fx_emoji in fx_defs:
        m = re.match(fx_pattern, text_lower)
        if not m:
            continue
        amount_str = m.group(1)
        amount = parse_number(amount_str) if amount_str else Decimal('1')
        if amount is None:
            amount = Decimal('1')

        fx_to_irr = fx_getter()
        if fx_to_irr is None:
            bot.reply_to(message, add_timestamp(T(user_id, 'rate_unavailable')), parse_mode='HTML')
            return True
        result_toman = amount * Decimal(str(fx_to_irr))

        user_lang = db_get_lang(user_id)
        amount_formatted = format_fiat(amount)
        result_formatted = format_fiat(result_toman, decimals=0)
        amount_formatted = format_for_locale(amount_formatted, user_lang)
        result_formatted = format_for_locale(result_formatted, user_lang)

        reply_text = T(user_id, f'{fx_code.lower()}_conversion',
                       amount=amount_formatted, result=result_formatted)

        bot.reply_to(message, add_timestamp(reply_text), parse_mode='HTML')
        if is_group:
            query_key = f"fiat:{fx_code.lower()}_to_toman:{text_lower}"
            raw_data = {'rate': fx_to_irr, 'amount': str(amount)}
            set_group_cached_query(chat_id, query_key, raw_data)
        return True

    return False

def _handle_text_gold(message, user_id, text_lower):
    is_group = message.chat.type in ('group', 'supergroup')
    chat_id = message.chat.id
    # Gold → USD (gram to dollar conversion)
    gold_pattern = r'^([\d.,۰-۹٬٫]+)\s*(gold|طل|طلا)\s*(?:to|به)?\s*(\$|usd|dollar|دلار)?$'
    m = re.match(gold_pattern, text_lower)
    if m:
        amount = parse_number(m.group(1))
        if amount is None:
            amount = Decimal('1')

        gold_prices = get_gold_prices()
        xau_price = gold_prices.get('xau')
        if not xau_price:
            bot.reply_to(message, T(user_id, 'price_fetch_fail'))
            return True

        gram_price = Decimal(str(xau_price)) / Decimal('31.1035')
        result_usd = amount * gram_price

        user_lang = db_get_lang(user_id)
        result_formatted = format_for_locale(format_fiat(result_usd), user_lang)
        amount_formatted = format_for_locale(format_fiat(amount), user_lang)
        msg = T(user_id, 'gold_conv_hdr', amount=amount_formatted) + quote(f"💵 = ${result_formatted}")
        bot.reply_to(
            message,
            add_timestamp(msg),
            parse_mode='HTML'
        )
        if is_group:
            query_key = f"gold:gram_to_usd:{text_lower}"
            raw_data = {'xau_price': xau_price, 'amount': str(amount)}
            set_group_cached_query(chat_id, query_key, raw_data)
        return True

    return False

def _handle_text_wallet_and_tx(message, user_id, text):
    is_group = message.chat.type in ('group', 'supergroup')
    chat_id = message.chat.id
    
    # TRON wallet address
    if re.match(r'^[A-Za-z0-9]{34}$', text):
        if is_valid_tron_address(text):
            bot.send_chat_action(message.chat.id, 'typing')
            result = get_tron_wallet_trx(text, user_id)
            bot.reply_to(message, add_timestamp(result), parse_mode='HTML')
            if is_group:
                query_key = f"wallet:{text}"
                set_group_cached_query(chat_id, query_key, result)
        else:
            bot.reply_to(message, T(user_id, 'invalid_tron_addr'))
        return True

    # TON wallet address (48 chars, starts with EQ or UQ)
    if len(text) == 48 and text[:2] in ['EQ', 'UQ']:
        if is_valid_ton_address(text):
            bot.send_chat_action(message.chat.id, 'typing')
            result = get_ton_wallet_balance(text, user_id)
            bot.reply_to(message, add_timestamp(result), parse_mode='HTML')
            if is_group:
                query_key = f"wallet:{text}"
                set_group_cached_query(chat_id, query_key, result)
            return True
        else:
            bot.reply_to(message, T(user_id, 'invalid_address'))
            return True

    # Transaction hash or link - detect chain from URL or try both for bare hashes
    tronscan_match = re.match(r'https?://tronscan\.org/#/transaction/([A-Fa-f0-9]{64})', text)
    tonscan_match = re.match(r'https?://tonscan\.org/tx/([A-Fa-f0-9]{64})', text)
    tonviewer_match = re.match(r'https?://tonviewer\.com/transaction/([A-Fa-f0-9]{64})', text)
    bare_hash_match = re.match(r'^[A-Fa-f0-9]{64}$', text)

    tx_hash = None
    detected_chain = None  # 'tron', 'ton', or None for unknown

    if tronscan_match:
        tx_hash = tronscan_match.group(1)
        detected_chain = 'tron'
    elif tonviewer_match:
        tx_hash = tonviewer_match.group(1)
        detected_chain = 'ton'
    elif tonscan_match:
        tx_hash = tonscan_match.group(1)
        detected_chain = 'ton'
    elif bare_hash_match:
        tx_hash = text
        detected_chain = None  # Unknown - will try both

    if tx_hash:
        bot.send_chat_action(message.chat.id, 'typing')
        
        if detected_chain == 'tron':
            # TRON-specific URL - only try TRON
            tron_result = get_tron_transaction_details(tx_hash, user_id)
            bot.reply_to(message, add_timestamp(tron_result), parse_mode='HTML')
            if is_group:
                query_key = f"txhash:tron:{tx_hash}"
                set_group_cached_query(chat_id, query_key, {'tron': tron_result})
            return True
        elif detected_chain == 'ton':
            # TON-specific URL - only try TON
            ton_result = get_ton_transaction_details(tx_hash, user_id)
            bot.reply_to(message, add_timestamp(ton_result), parse_mode='HTML')
            if is_group:
                query_key = f"txhash:ton:{tx_hash}"
                set_group_cached_query(chat_id, query_key, {'ton': ton_result})
            return True
        else:
            # Bare hash - try both chains, but label results clearly
            # Try TRON first
            tron_result = get_tron_transaction_details(tx_hash, user_id)
            tron_success = tron_result and "not found" not in tron_result.lower() and "error" not in tron_result.lower()
            
            # Try TON
            ton_result = get_ton_transaction_details(tx_hash, user_id)
            ton_success = ton_result and "not found" not in ton_result.lower() and "error" not in ton_result.lower()
            
            if tron_success and not ton_success:
                bot.reply_to(message, add_timestamp(tron_result), parse_mode='HTML')
                if is_group:
                    query_key = f"txhash:tron:{tx_hash}"
                    set_group_cached_query(chat_id, query_key, {'tron': tron_result})
                return True
            elif ton_success and not tron_success:
                bot.reply_to(message, add_timestamp(ton_result), parse_mode='HTML')
                if is_group:
                    query_key = f"txhash:ton:{tx_hash}"
                    set_group_cached_query(chat_id, query_key, {'ton': ton_result})
                return True
            elif tron_success and ton_success:
                # Both succeeded - this shouldn't happen but show both with clear labels
                combined = (
                    f"⚠️ <b>Hash matches both TRON and TON networks</b>\n\n"
                    f"━━━ <b>TRON</b> ━━━\n{tron_result}\n\n"
                    f"━━━ <b>TON</b> ━━━\n{ton_result}"
                )
                bot.reply_to(message, add_timestamp(combined), parse_mode='HTML')
                if is_group:
                    query_key = f"txhash:both:{tx_hash}"
                    set_group_cached_query(chat_id, query_key, {'tron': tron_result, 'ton': ton_result})
                return True
            else:
                # Neither succeeded - show both error messages
                combined = (
                    f"❌ <b>Transaction not found on either network</b>\n\n"
                    f"<b>TRON:</b> {tron_result}\n\n"
                    f"<b>TON:</b> {ton_result}"
                )
                bot.reply_to(message, add_timestamp(combined), parse_mode='HTML')
                return True

    return False

def _handle_text_standalone(message, user_id, text, text_lower):
    # Standalone "try" → show TRY rate
    if text_lower in ('try', 'lira', 'tl', '₺') and not re.search(r'\d', text):
        bot.send_chat_action(message.chat.id, 'typing')
        try_to_irr = get_try_to_irr()
        if try_to_irr is None:
            bot.reply_to(message, add_timestamp(T(user_id, 'rate_unavailable')), parse_mode='HTML')
            return True
        user_lang = db_get_lang(user_id)
        toman_label = "Toman" if user_lang == 'en' else "تومان"
        rate_formatted = format_for_locale(format_fiat(Decimal(str(try_to_irr)), decimals=0), user_lang)
        bot.reply_to(
            message,
            add_timestamp(T(user_id, 'try_rate', rate=rate_formatted)),
            parse_mode='HTML'
        )
        return True

    # Standalone "gold" / "طلا" / "xau" → show gold price
    if text_lower in ('gold', 'طل', 'طلا', 'xau') and not re.search(r'\d', text):
        bot.send_chat_action(message.chat.id, 'typing')
        msg = _build_gold_message(user_id)
        if not msg:
            bot.reply_to(message, T(user_id, 'price_fetch_fail'))
            return True
        bot.reply_to(message, add_timestamp(msg), parse_mode='HTML')
        return True

    return False

def _handle_text_crypto(message, user_id, text, text_lower):
    # Single crypto symbol → instant sparkline + chart
    parts_raw = text.split()
    text_clean = parts_raw[0] if parts_raw else text
    exchange_override = None
    if len(parts_raw) == 2 and parts_raw[1].startswith('@'):
        exch = parts_raw[1][1:].lower()
        if exch in EXCHANGES or exch == 'coingecko':
            exchange_override = exch
            text = text_clean
    if not re.search(r'\d', text) and len(text.split()) == 1:
        crypto = detect_currency(text)
        if crypto and crypto in CRYPTO_LIST:
            bot.send_chat_action(message.chat.id, 'typing')
            crypto_name = CRYPTO_LIST.get(crypto, crypto)
            sym = _sym(crypto)
            if crypto == 'telegram-stars':
                refresh_kb = None
            else:
                refresh_kb = types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, 'btn_refresh'),   callback_data=f"refresh_{crypto}"),
                    types.InlineKeyboardButton(T(user_id, 'btn_add_coin'),  callback_data=f"hpick_{crypto}"),
                ]])

            exch = exchange_override or get_user_exchange(user_id)
            price_usd = get_exchange_price(crypto, exch)

            usd_to_irr = cache_get('usd_to_irr') or get_usd_to_irr()
            if not price_usd:
                bot.reply_to(message, T(user_id, 'price_fetch_fail'))
                return True
            toman_line = _secondary_currency_line(user_id, price_usd, usd_to_irr)

            # Cache raw data for group deduplication
            is_group = message.chat.type in ('group', 'supergroup')
            if is_group:
                query_key = f"crypto:{text_lower}"
                raw_data = {
                    'price': price_usd,
                    'change': None,  # Could fetch 24h change if needed
                    'name': crypto_name,
                    'sym': sym,
                }
                set_group_cached_query(message.chat.id, query_key, raw_data)

            kb = None
            if crypto != 'telegram-stars':
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("📊 Chart", callback_data=f"chart_{crypto}_30d"))
            # Shared price block (same as inline results)
            msg = _coin_price_block(
                user_id, crypto_name, _fmt_price_with_exchange(price_usd, exch),
                toman_str=toman_line,
                extra_rows=_inline_fiat_lines(price_usd, usd_to_irr),
            )
            bot.reply_to(
                message,
                add_timestamp(msg),
                parse_mode='HTML',
                reply_markup=kb
            )
            return True

    # Amount + single crypto (e.g. "10 trx")
    crypto_amount_match = re.match(r'^(\d+(?:\.\d+)?)\s*(\w+)$', text_lower)
    if crypto_amount_match:
        amount = float(crypto_amount_match.group(1))
        crypto = detect_currency(crypto_amount_match.group(2), check_u_alias=True)
        if crypto and crypto in CRYPTO_LIST:
            bot.send_chat_action(message.chat.id, 'typing')
            price_usd  = get_crypto_price(crypto)
            usd_to_irr = get_usd_to_irr()
            if price_usd:
                value_usd  = amount * price_usd
                sym = _sym(crypto)
                toman_line = f"💰 {value_usd * usd_to_irr:,.0f} {T(user_id, 'toman_label')}" if usd_to_irr else ""
                
                # Cache raw data for group deduplication
                is_group = message.chat.type in ('group', 'supergroup')
                if is_group:
                    query_key = f"amount_crypto:{text_lower}"
                    raw_data = {
                        'price': value_usd,
                        'change': None,
                        'name': CRYPTO_LIST.get(crypto, crypto),
                        'sym': sym,
                    }
                    set_group_cached_query(message.chat.id, query_key, raw_data)
                
                rows = [f"💵 {fmt_price(value_usd)}"]
                if toman_line:
                    rows.append(toman_line)
                rows.extend(_inline_fiat_lines(value_usd, usd_to_irr))
                msg = f"💰 <b>{amount:,g} {sym}</b>\n" + quote("\n".join(rows))
                bot.reply_to(
                    message,
                    add_timestamp(msg),
                    parse_mode='HTML'
                )
            else:
                bot.reply_to(message, T(user_id, 'price_fetch_fail'))
            return True

    # Conversion patterns
    if len(text.split()) <= 4:
        patterns = [
            r'^(\d+(?:\.\d+)?)\s*(\w+)\s*(?:to|به)\s*(\w+)$',
            r'^(\d+(?:\.\d+)?)\s*(\w+)\s+(\w+)$'
        ]
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                amount = float(match.group(1))
                src = detect_currency(match.group(2), check_u_alias=True)
                dst = detect_currency(match.group(3), check_u_alias=True)
                if not src or not dst:
                    continue
                bot.send_chat_action(message.chat.id, 'typing')
                result, error_msg = convert_amount(amount, src, dst)
                if result is not None:
                    src_sym = _sym(src)
                    dst_sym = _sym(dst)
                    if dst == 'usd':
                        result_str = fmt_price(result)
                        display_to_sym = ""
                    elif dst == 'toman':
                        result_str = f"{result:,.0f}"
                        display_to_sym = T(user_id, 'toman_label')
                    else:
                        result_str = f"{result:,.6g}"
                        display_to_sym = dst_sym
                    # Same layout as the convert wizard: header + quoted toman note
                    toman_note = ""
                    usd_to_irr = cache_get('usd_to_irr') or get_usd_to_irr()
                    if dst == 'usd' and usd_to_irr:
                        toman_note = T(user_id, 'convert_toman_note', irr=f"{result * usd_to_irr:,.0f}")
                    elif dst == 'toman' and usd_to_irr:
                        usd_val = result / usd_to_irr
                        toman_note = T(user_id, 'convert_toman_approx', usd=fmt_price(usd_val), irr=f"{result:,.0f}")
                    msg = T(user_id, 'convert_result', amount=f"{amount:,g}", from_sym=src_sym, result=result_str, to_sym=display_to_sym)
                    if toman_note:
                        msg += quote(toman_note)
                    bot.reply_to(
                        message,
                        add_timestamp(msg),
                        parse_mode='HTML'
                    )
                else:
                    bot.reply_to(message, T(user_id, 'convert_fail', err=error_msg))
                return True


    return False

@bot.message_handler(func=lambda message: True, content_types=["text"])
@rate_limit_check
@loading_indicator
def handle_text(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    is_group = message.chat.type in ('group', 'supergroup')
    
    if not message.text:
        return  # ignore stickers, photos, voice messages, etc.
    text_original = message.text.strip()
    # Normalize Persian-Indic digits/operators → ASCII so all regex matches work
    text = _normalize_persian(text_original)
    text_lower = text.lower()

    state = get_user_state(user_id)

    if state and _handle_text_state(message, user_id, text, text_lower, text_original, state):
        return

# For group plain-text queries, use deduplication cache for common queries
    query_key = None
    query_type = None
    if is_group and not state:
        # Create a cache key for common query types
        if re.match(r'^[A-Za-z0-9]{3,10}$', text_lower) and text_lower in CRYPTO_ALIASES:
            query_key = f"crypto:{text_lower}"
            query_type = 'crypto_price'
        elif re.match(r'^\d+(?:\.\d+)?\s+\w+$', text_lower):
            query_key = f"amount_crypto:{text_lower}"
            query_type = 'crypto_price'
        elif re.match(r'^[A-Za-z0-9]{34}$', text) or (len(text) == 48 and text[:2] in ('EQ', 'UQ')):
            query_key = f"wallet:{text}"
            query_type = 'wallet_check'
        elif re.match(r'^[A-Fa-f0-9]{64}$', text):
            query_key = f"txhash:{text}"
            query_type = 'tx_check'
    
    # Try to get cached raw data
    cached_raw = None
    if query_key:
        cached_raw = get_group_cached_query(chat_id, query_key)
        if cached_raw is not None:
            # Format cached raw data for this user's language
            formatted = _format_cached_group_result(query_type, cached_raw, user_id, text_lower, text_original)
            try:
                bot.reply_to(message, formatted, parse_mode='HTML')
            except Exception:
                pass
            return

    if _handle_text_math(message, user_id, text, text_lower, text_original):
        return

    if _handle_text_fiat(message, user_id, text_lower):
        return

    if _handle_text_gold(message, user_id, text_lower):
        return

    if _handle_text_wallet_and_tx(message, user_id, text):
        return

    if _handle_text_standalone(message, user_id, text, text_lower):
        return

    if _handle_text_crypto(message, user_id, text, text_lower):
        return

# ─────────────────────────────────────────────
# Alert + digest callbacks
# ─────────────────────────────────────────────
def _handle_alert_callbacks(call, data, user_id):
    if data.startswith("alertdel_"):
        alert_id = int(data.split("_")[1])
        db_delete_alert(alert_id, user_id)
        alerts = db_get_alerts(user_id)
        if not alerts:
            try:
                bot.edit_message_text(
                    T(user_id, 'alert_deleted_last'),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=types.InlineKeyboardMarkup([[
                        types.InlineKeyboardButton(T(user_id, 'btn_add_alert'), callback_data="alrt_new")
                    ]])
                )
            except Exception:
                pass
        else:
            keyboard = []
            above_w = T(user_id, 'above_word')
            below_w = T(user_id, 'below_word')
            header = T(user_id, 'alerts_header', count=len(alerts), max=MAX_ALERTS_PER_USER)
            body = []
            # Batch-fetch all alert prices at once
            alert_ids = [a['crypto_id'] for a in alerts if a['crypto_id'] in CRYPTO_LIST]
            if alert_ids:
                _fetch_prices_batch(','.join(set(alert_ids)))
            for a in alerts:
                arrow = '📈' if a['direction'] == 'above' else '📉'
                cur = get_crypto_price(a['crypto_id'])
                dword = above_w if a['direction'] == 'above' else below_w
                if cur:
                    pct_str = f"{abs((a['target_price']-cur)/cur)*100:.1f}"
                    dist = f"  <i>{T(user_id, 'away_pct', pct=pct_str)}</i>"
                else:
                    dist = ""
                body.append(f"{arrow} <b>{a['symbol']}</b> {dword} <b>{fmt_price(a['target_price'])}</b>{dist}")
                keyboard.append([types.InlineKeyboardButton(
                    f"🗑  {a['symbol']} {dword} {fmt_price(a['target_price'])}",
                    callback_data=f"alertdel_{a['id']}"
                )])
            text = header + (quote("\n".join(body)) if body else "")
            keyboard.append([
                types.InlineKeyboardButton(T(user_id, 'btn_add_alert'),  callback_data="alrt_new"),
                types.InlineKeyboardButton(T(user_id, 'btn_delete_all'), callback_data="alertdelall"),
            ])
            _edit_call(call, 
            text, reply_markup=types.InlineKeyboardMarkup(keyboard)
            )
        _answer_call(call, T(user_id, 'alert_deleted'))
        return True

    if data == "alertdelall":
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM alerts WHERE user_id=?", (user_id,))
            conn.commit()
        try:
            bot.edit_message_text(
                T(user_id, 'alerts_all_deleted'),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, 'btn_set_new_alert'), callback_data="alrt_new")
                ]])
            )
        except Exception:
            pass
        _answer_call(call, T(user_id, 'alerts_all_deleted'))
        return True

    if data.startswith("digest_"):
        action = data[len("digest_"):]
        pref = db_get_digest(user_id) or {'enabled': False, 'hour': 9}
        if action == "on":
            db_set_digest(user_id, True, pref['hour'])
            _answer_call(call, T(user_id, 'digest_on_toast'))
        elif action == "off":
            db_set_digest(user_id, False, pref['hour'])
            _answer_call(call, T(user_id, 'digest_off_toast'))
        elif action == "custom":
            _answer_call(call)
            set_user_state(user_id, 'digest_custom_hour')
            kb = types.InlineKeyboardMarkup([[
                types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="digest_cancel")
            ]])
            bot.send_message(
                call.message.chat.id,
                T(user_id, 'digest_time_prompt'),
                parse_mode='HTML',
                reply_markup=kb
            )
            return True
        elif action.startswith("h"):
            try:
                hour = int(action[1:])
                db_set_digest(user_id, pref['enabled'], hour)
                _answer_call(call, T(user_id, 'digest_time_set', hour=f"{hour:02d}"))
            except ValueError:
                _answer_call(call, T(user_id, 'invalid_hour'))
                return True
        # Refresh the digest message
        pref    = db_get_digest(user_id) or {'enabled': False, 'hour': 9}
        enabled = pref['enabled']
        hour    = pref['hour']
        status  = T(user_id, 'digest_enabled') if enabled else T(user_id, 'digest_disabled')
        _edit_call(call, 
        T(user_id, 'digest_header') +
                T(user_id, 'digest_status', status=status, hour=f"{hour:02d}"), reply_markup=_build_digest_keyboard(enabled, hour, user_id)
        )
        return True

    return False


def _send_digest_to_user(user_id: int):
    """Send daily digest to a specific user. Returns True when the message
    was actually sent (used by the scheduler to mark the slot as done)."""
    try:
        saved = db_get_holdings(user_id)
        if not saved:
            return True  # nothing to send — the slot is "done" either way
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)

        # Batch-fetch prices + 24h change for all held coins (seeds individual caches too)
        crypto_ids = [detect_currency(s.lower()) for s in saved if detect_currency(s.lower())]
        change_map = {}
        if crypto_ids:
            batch = _fetch_prices_batch(','.join(set(crypto_ids)))
            for cid in crypto_ids:
                change_map[cid] = batch.get(cid, {}).get('usd_24h_change')

        total_usd = 0.0
        rows = []
        for symbol, amount in saved.items():
            cid = detect_currency(symbol.lower())
            if not cid:
                continue
            price = get_crypto_price(cid)
            if not price:
                continue
            value = amount * price
            total_usd += value
            change_str = _chg_str(change_map.get(cid))
            pnl_str = ""
            buy = buy_prices.get(symbol.upper())
            if buy and buy > 0:
                pnl_usd = (price - buy) * amount
                sign = "+" if pnl_usd >= 0 else ""
                pnl_usd_abs = abs(pnl_usd)
                pnl_str = f"  <i>({sign}${fmt_price(pnl_usd_abs)} P&amp;L)</i>"
            rows.append(f"🪙 <b>{symbol}</b>  {fmt_price(value)}{change_str}{pnl_str}")

        if usd_to_irr:
            total_irr = total_usd * usd_to_irr
            total_line = _T_cached(user_id, 'digest_total', usd=fmt_price(total_usd), irr=f"{total_irr:,.0f}")
        else:
            total_line = _T_cached(user_id, 'digest_total', usd=fmt_price(total_usd), irr="N/A")
        text = (
            _T_cached(user_id, 'digest_morning') +
            (quote("\n".join(rows)) if rows else "") +
            total_line +
            f"\n<i>📅 {datetime.now(IRAN_TZ).strftime('%b %d, %Y  %H:%M')} (Tehran)</i>"
        )
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(_T_cached(user_id, 'btn_portfolio'), callback_data="show_holdings"),
            types.InlineKeyboardButton(_T_cached(user_id, 'btn_alerts'),    callback_data="show_alerts"),
        ]])
        bot.send_message(user_id, text, parse_mode='HTML', reply_markup=kb)
        return True
    except Exception as e:
        logger.error(f"Digest send failed for user {user_id}: {e}")
        return False


# ─────────────────────────────────────────────
# Background: alert checker (runs every 60s)
# ─────────────────────────────────────────────
def _check_alerts_once():
    """Fetch prices for all alerts, fire + delete the ones whose condition is met."""
    alerts = db_get_all_alerts()
    if alerts:
        # ── Batch-fetch all needed prices in one CoinGecko call ───
        unique_ids = list({a['crypto_id'] for a in alerts})
        price_map: dict[str, float] = {}
        try:
            # Use the shared rate-limited batch fetcher instead of a raw
            # CoinGecko call that competed with user traffic for the rate budget.
            batch = _fetch_prices_batch(','.join(unique_ids)) or {}
            for cid in unique_ids:
                p = (batch.get(cid) or {}).get('usd')
                if p:
                    price_map[cid] = p
                    cache_set(cid, p)
        except Exception as e:
            logger.warning(f"Alert checker batch fetch failed: {e} - falling back to cache")
            # Fall back to individually cached values
            for cid in unique_ids:
                cached = cache_get(cid)
                if cached:
                    price_map[cid] = cached

        for a in alerts:
            price = price_map.get(a['crypto_id'])
            if price is None:
                continue
            triggered = (
                (a['direction'] == 'above' and price >= a['target_price']) or
                (a['direction'] == 'below' and price <= a['target_price'])
            )
            if triggered:
                arrow = '📈' if a['direction'] == 'above' else '📉'
                try:
                    _uid = a['user_id']
                    _dword = _T_cached(_uid, 'above_word') if a['direction'] == 'above' else _T_cached(_uid, 'below_word')
                    kb = types.InlineKeyboardMarkup([[
                        types.InlineKeyboardButton(_T_cached(_uid, 'btn_set_new_alert'), callback_data='alrt_new'),
                        types.InlineKeyboardButton(_T_cached(_uid, 'btn_holdings'),     callback_data='show_holdings'),
                    ]])
                    bot.send_message(
                        _uid,
                        _T_cached(_uid, 'alert_triggered', arrow=arrow, sym=a['symbol'],
                          price=fmt_price(price), direction=_dword,
                          target=fmt_price(a['target_price'])),
                        parse_mode='HTML',
                        reply_markup=kb
                    )
                    # Delete on permanent errors too: a 403 (user blocked the
                    # bot) used to retry forever, logging an error every cycle.
                    db_delete_alert_by_id(a['id'])
                    logger.info(
                        f"Alert {a['id']} fired for user {a['user_id']}: "
                        f"{a['symbol']} {a['direction']} {fmt_price(a['target_price'])}"
                    )
                except ApiTelegramException as e:
                    err = str(e).lower()
                    if 'bot was blocked' in err or 'chat not found' in err or 'forbidden' in err \
                            or getattr(e, 'error_code', None) == 403:
                        logger.info(
                            f"User {a['user_id']} blocked the bot; removing alert {a['id']}"
                        )
                        db_delete_alert_by_id(a['id'])
                    else:
                        logger.error(f"Could not send alert to user {a['user_id']}: {e}")
                except Exception as e:
                    logger.error(f"Could not send alert to user {a['user_id']}: {e}")


# ─────────────────────────────────────────────
# Background: daily digest sender
# (the old dedicated _alert_checker_loop/_digest_loop threads were removed —
#  the unified _background_scheduler_loop below owns both jobs now, and
#  accidentally starting both would double-fire alerts/digests)
# ─────────────────────────────────────────────
_digest_last_sent: dict = {}  # user_id -> "YYYY-MM-DD HH" of last send


def _run_digests_once():
    """Send the digest to every opted-in user whose chosen hour (Iran time) is now.

    Each user gets at most one digest per hour slot, regardless of how often
    this is called.
    """
    now = datetime.now(IRAN_TZ)
    slot = now.strftime('%Y-%m-%d %H')
    for u in db_get_all_digest_users():
        uid, hour = u['user_id'], u['hour']
        if now.hour == hour and _digest_last_sent.get(uid) != slot:
            # Mark the slot only AFTER the send succeeds — otherwise a failed
            # send silently skips the user's digest for that hour slot.
            def _send_and_mark(uid=uid, slot=slot):
                if _send_digest_to_user(uid):
                    _digest_last_sent[uid] = slot
            threading.Thread(target=_send_and_mark, daemon=True).start()


def global_exception_handler(exc_type, exc_value, exc_traceback):
    logger.error("Unhandled exception in main thread", exc_info=(exc_type, exc_value, exc_traceback))


def thread_exception_handler(args):
    logger.error(f"Unhandled exception in thread {args.thread.name}",
                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


sys.excepthook = global_exception_handler
threading.excepthook = thread_exception_handler


# Patch telebot to catch handler exceptions and reply gracefully
_original_process_new_updates = bot.process_new_updates


def _safe_process_new_updates(updates):
    for update in updates:
        try:
            _original_process_new_updates([update])
        except Exception as e:
            logger.error(f"Unhandled exception processing update: {e}", exc_info=True)
            try:
                if update.message:
                    bot.reply_to(
                        update.message,
                        T(update.message.from_user.id if update.message and update.message.from_user else 0, 'something_went_wrong')
                    )
            except Exception:
                pass  # don't let the reply attempt crash us too


bot.process_new_updates = _safe_process_new_updates


# ═══════════════════════════════════════════════════════════════
# Telegram Mini App (webapp) - static frontend + JSON API
#   * Serves the frontend in ./webapp and /api/* endpoints.
#   * Every /api call is authenticated with Telegram initData
#     (HMAC-SHA256 signed with the bot token).
#   * Runs either as:
#       - WSGI app `_webapp_wsgi` (cPanel/Passenger gives it HTTPS
#         automatically via a Python app subdomain), or
#       - a local HTTP server on WEBAPP_HOST:PORT when using
#         `python3 bot.py` directly.
# ═══════════════════════════════════════════════════════════════
WEBAPP_HOST = os.getenv('WEBAPP_HOST', '127.0.0.1')
WEBAPP_PORT = int(os.getenv('WEBAPP_PORT', '8080'))
# WEBAPP_ALLOW_DEV defaults to False for security - must be explicitly enabled
WEBAPP_ALLOW_DEV = os.getenv('WEBAPP_ALLOW_DEV', '').lower() in ('1', 'true', 'yes')
if WEBAPP_ALLOW_DEV:
    logger.warning(
        "⚠️  WEBAPP_ALLOW_DEV is ENABLED — anyone passing ?dev_uid=<id> can "
        "impersonate any user and read/delete their data. NEVER enable in production!"
    )
WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'webapp')
WEBAPP_JSON_CT = 'application/json; charset=utf-8'
WEBAPP_MIME = {
    '.html':  'text/html; charset=utf-8',
    '.css':   'text/css; charset=utf-8',
    '.js':    'application/javascript; charset=utf-8',
    '.json':  'application/json; charset=utf-8',
    '.png':   'image/png',
    '.svg':   'image/svg+xml',
    '.ico':   'image/x-icon',
    '.woff2': 'font/woff2',
}

# Content Security Policy for WebApp
WEBAPP_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://api.telegram.org; "
    "frame-ancestors https://web.telegram.org https://telegram.org;"
)


def _webapp_validate_init_data(init_data):
    """Verify Telegram Mini App initData and return the user id (or None)."""
    if not init_data:
        return None
    try:
        params = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received_hash = params.pop('hash', None)
    if not received_hash:
        return None
    # Reject initData older than 24h (Telegram spec) so stolen tokens can't
    # be replayed indefinitely. auth_date is required and part of the HMAC.
    auth_date = params.get('auth_date')
    if not auth_date:
        return None
    try:
        auth_ts = int(auth_date)
    except (TypeError, ValueError):
        return None
    if abs(time.time() - auth_ts) > 86400:
        return None
    data_check = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode('utf-8'), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        return None
    try:
        user = json.loads(params.get('user', '{}'))
    except Exception:
        return None
    uid = user.get('id')
    return int(uid) if isinstance(uid, (int, float, str)) else None


def _webapp_coin_meta(cid):
    raw = CRYPTO_LIST.get(cid, cid)
    m = re.match(r'^(.*?)\s*\(([^)]+)\)\s*$', raw)
    if m:
        head, sym = m.group(1).strip(), m.group(2)
    else:
        head, sym = raw, raw.upper()
    parts = head.split()
    if parts and parts[0] in ('🪙', '⭐', '💎', '👑', '🔥', '💰', '🌐'):
        return parts[0], ' '.join(parts[1:]) or sym, sym
    return '🪙', head or sym, sym


def _webapp_sparklines(cids):
    cached = cache_get('webapp_sparklines')
    if cached:
        return cached
    try:
        ids = ','.join(cids)
        resp = session.get(
            'https://api.coingecko.com/api/v3/coins/markets'
            f'?vs_currency=usd&ids={ids}&sparkline=true', timeout=10)
        if resp.status_code != 200:
            return {}
        result = {c.get('id'): c.get('sparkline_in_7d', {}).get('price', []) for c in resp.json()}
        if result:
            cache_set('webapp_sparklines', result, ttl=600)
        return result
    except Exception as e:
        logger.error(f"WebApp sparklines failed: {e}")
        return {}


def _webapp_prices():
    # Use cached prices primarily, fetch fresh only if needed
    # Remove USDT (tether) and Telegram Stars
    excluded_cids = {'telegram-stars', 'tether'}
    real_cids = [c for c in CRYPTO_LIST if c not in excluded_cids]
    batch = _fetch_prices_batch(','.join(real_cids)) or {}
    spark = _webapp_sparklines(real_cids)
    usd_to_irr = cache_get('usd_to_irr') or get_usd_to_irr()
    coins = []
    for cid in CRYPTO_LIST:
        if cid in excluded_cids:
            continue
        icon, name, sym = _webapp_coin_meta(cid)
        info = batch.get(cid, {})
        price = info.get('usd') or get_crypto_price(cid)
        change = info.get('usd_24h_change')
        coins.append({
            'cid': cid, 'sym': sym, 'name': name, 'icon': icon,
            'price': price, 'change': change,
            'sparkline': (spark.get(cid) or [])[-32:],
        })
    return {'ok': True, 'usd_to_irr': usd_to_irr, 'coins': coins}


def _webapp_currency():
    cached_currency = cache_get('webapp_currency')
    if cached_currency:
        return cached_currency
    usd_to_irr = cache_get('usd_to_irr') or get_usd_to_irr()
    try:
        resp = session.get('https://api.exchangerate.host/latest?base=USD', timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            rates = data.get('rates', {})
            # Select major currencies
            selected = {
                'IRR': usd_to_irr or rates.get('IRR'),
                'EUR': rates.get('EUR'),
                'GBP': rates.get('GBP'),
                'TRY': rates.get('TRY'),
                'AED': rates.get('AED'),
                'CNY': rates.get('CNY'),
                'RUB': rates.get('RUB'),
                'INR': rates.get('INR'),
                'JPY': rates.get('JPY'),
                'KRW': rates.get('KRW'),
            }
            # Filter out None values
            selected = {k: v for k, v in selected.items() if v is not None}
            names = {
                'IRR': 'Iranian Rial',
                'EUR': 'Euro',
                'GBP': 'British Pound',
                'TRY': 'Turkish Lira',
                'AED': 'UAE Dirham',
                'CNY': 'Chinese Yuan',
                'RUB': 'Russian Ruble',
                'INR': 'Indian Rupee',
                'JPY': 'Japanese Yen',
                'KRW': 'South Korean Won',
            }
            payload = {
                'ok': True,
                'rates': selected,
                'names': names,
                'updated': data.get('timestamp', int(time.time())) * 1000,
            }
            cache_set('webapp_currency', payload, ttl=300)
            return payload
    except Exception as e:
        logger.error(f"Currency fetch failed: {e}")
    return {'ok': True, 'rates': {'IRR': usd_to_irr} if usd_to_irr else {}, 'names': {'IRR': 'Iranian Rial'}, 'updated': int(time.time() * 1000)}


def _webapp_portfolio(uid):
    holdings = db_get_holdings(uid) or {}
    buy_prices = db_get_buy_prices(uid) or {}
    sym_to_cid = {_sym(cid): cid for cid in CRYPTO_LIST}
    items = []
    total_value = total_cost = 0.0
    for sym, amount in holdings.items():
        cid = sym_to_cid.get(sym)
        if cid is None:
            continue
        _, name, _ = _webapp_coin_meta(cid)
        price = get_crypto_price(cid) or 0
        value = float(amount) * price
        buy = buy_prices.get(sym)
        cost = float(amount) * float(buy) if buy else 0.0
        pnl = (value - cost) if cost else None
        pnl_pct = (pnl / cost * 100) if cost else None
        total_value += value
        total_cost += cost
        items.append({
            'cid': cid, 'sym': sym, 'name': name, 'amount': float(amount),
            'price': price, 'buy': buy, 'value': value, 'pnl': pnl, 'pnl_pct': pnl_pct,
        })
    items.sort(key=lambda x: x['value'], reverse=True)
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else None
    return {
        'ok': True, 'items': items,
        'total_value': total_value, 'total_cost': total_cost,
        'total_pnl': total_pnl, 'total_pnl_pct': total_pnl_pct,
    }


def _webapp_market():
    g = _fetch_coingecko_global() or {}
    fng = _fetch_fear_greed()
    trend = _fetch_trending() or []
    gl = _fetch_gainers_losers() or {}

    def _movers(coins):
        return [
            {'id': c.get('id'), 'name': c.get('name'), 'symbol': (c.get('symbol') or '').upper(),
             'price': c.get('current_price'), 'change': c.get('price_change_percentage_24h')}
            for c in (coins or [])
        ]

    return {
        'ok': True,
        'global': {
            'total_market_cap_usd': (g.get('total_market_cap') or {}).get('usd'),
            'total_volume_usd': (g.get('total_volume') or {}).get('usd'),
            'market_cap_percentage_btc': (g.get('market_cap_percentage') or {}).get('btc'),
            'market_cap_percentage_eth': (g.get('market_cap_percentage') or {}).get('eth'),
            'active_cryptocurrencies': g.get('active_cryptocurrencies'),
            'market_cap_change_percentage_24h_usd': g.get('market_cap_change_percentage_24h_usd'),
        },
        'fear_greed': fng or {},
        'trending': [
            {'id': t.get('id'), 'name': t.get('name'), 'symbol': t.get('symbol'),
             'market_cap_rank': t.get('market_cap_rank'),
             'price_btc': t.get('price_btc'), 'thumb': t.get('thumb')}
            for t in trend
        ],
        'gainers': _movers(gl.get('gainers')),
        'losers': _movers(gl.get('losers')),
        'usd_to_irr': get_usd_to_irr(),
    }


def _webapp_alerts(uid):
    alerts = db_get_alerts(uid) or []
    cids = [a['crypto_id'] for a in alerts if a['crypto_id'] in CRYPTO_LIST]
    batch = _fetch_prices_batch(','.join(cids)) if cids else {}
    items = []
    for a in alerts:
        info = batch.get(a['crypto_id']) if a['crypto_id'] in batch else {}
        price = info.get('usd') if info else get_crypto_price(a['crypto_id'])
        triggered = False
        if price is not None:
            triggered = (price >= a['target_price']) if a['direction'] == 'above' \
                else (price <= a['target_price'])
        items.append({
            'id': a['id'], 'cid': a['crypto_id'], 'sym': a['symbol'],
            'name': (CRYPTO_LIST.get(a['crypto_id']) or a['symbol']),
            'direction': a['direction'], 'target': a['target_price'],
            'price': price, 'triggered': triggered,
        })
    return {'ok': True, 'items': items}


def _webapp_trx_balance(address):
    try:
        if not _TRON_ADDR_RE.fullmatch(str(address)):
            return None
        resp = session.get(f'https://apilist.tronscan.org/api/account?address={address}', timeout=10)
        if resp.status_code != 200:
            return None
        sun = resp.json().get('balance', 0)
        return float(Decimal(str(sun)) / Decimal('1000000'))
    except Exception as e:
        logger.error(f"WebApp TRX balance failed for {address}: {e}")
        return None


def _webapp_ton_balance(address):
    try:
        if not _TON_ADDR_RE.fullmatch(str(address)):
            return None
        resp = session.get(f'https://toncenter.com/api/v2/getAddressInformation?address={address}', timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get('ok') and 'result' in data:
            nano = int(data['result'].get('balance', 0))
            return float(Decimal(str(nano)) / Decimal('1000000000'))
        return None
    except Exception as e:
        logger.error(f"WebApp TON balance failed for {address}: {e}")
        return None


def _webapp_wallets(uid):
    wallets = db_get_wallets(uid) or []
    trx_price = get_crypto_price('tron')
    ton_price = get_crypto_price('the-open-network')
    items = []
    total_usd = 0.0
    for addr in wallets:
        chain = detect_wallet_chain(addr)
        if chain == 'ton':
            bal = _webapp_ton_balance(addr)
            symbol = 'TON'
            price = ton_price
        else:
            bal = _webapp_trx_balance(addr)
            symbol = 'TRX'
            price = trx_price
        usd_val = (bal * price) if (bal is not None and price) else None
        if usd_val is not None:
            total_usd += usd_val
        items.append({
            'address': addr, 'chain': chain or 'tron', 'symbol': symbol,
            'balance': bal, 'balance_usd': usd_val,
        })
    return {
        'ok': True, 'items': items, 'total_usd': total_usd,
        'trx_price': trx_price, 'ton_price': ton_price,
    }


def _webapp_static(rel):
    """Serve a file from WEBAPP_DIR -> (status, headers, body_bytes)."""
    try:
        root_path = Path(WEBAPP_DIR).resolve()
        rel_path = rel.lstrip('/')
        full_path = root_path.joinpath(rel_path).resolve()

        if not full_path.is_relative_to(root_path):
            raise ValueError
    except (ValueError, RuntimeError):
        return 403, [('Content-Type', WEBAPP_JSON_CT), ('Cache-Control', 'no-store')], \
            json.dumps({'ok': False, 'error': 'forbidden'}).encode('utf-8')
    if not full_path.is_file():
        return 404, [('Content-Type', 'text/plain; charset=utf-8')], b'not found'
    ctype = WEBAPP_MIME.get(full_path.suffix.lower(), 'application/octet-stream')
    try:
        body = full_path.read_bytes()
    except Exception:
        return 404, [('Content-Type', 'text/plain; charset=utf-8')], b'not found'
    # Cache static assets for 1 hour, no-cache for HTML
    cache_control = 'public, max-age=3600' if full_path.suffix != '.html' else 'no-cache'
    return 200, [('Content-Type', ctype), ('Cache-Control', cache_control)], body


def _webapp_json(obj, status=200):
    return status, [('Content-Type', WEBAPP_JSON_CT), ('Cache-Control', 'no-store')], \
        json.dumps(obj, ensure_ascii=False).encode('utf-8')


def _webapp_api(path, uid, qs):
    try:
        if path == '/api/prices':
            return _webapp_json(_webapp_prices())
        if path == '/api/currency':
            return _webapp_json(_webapp_currency())
        if path == '/api/portfolio':
            return _webapp_json(_webapp_portfolio(uid))
        if path == '/api/market':
            return _webapp_json(_webapp_market())
        if path == '/api/alerts':
            return _webapp_json(_webapp_alerts(uid))
        if path == '/api/alerts/delete':
            try:
                alert_id = int(qs.get('alert_id', ''))
            except (TypeError, ValueError):
                return _webapp_json({'ok': False, 'error': 'bad request'}, 400)
            return _webapp_json({'ok': bool(db_delete_alert(alert_id, uid))})
        if path == '/api/wallets':
            return _webapp_json(_webapp_wallets(uid))
        if path == '/api/wallets/delete':
            address = qs.get('address', '')
            ok = bool(db_remove_wallet(uid, address)) if address else False
            return _webapp_json({'ok': ok})
        return _webapp_json({'ok': False, 'error': 'not found'}, 404)
    except Exception as e:
        logger.error(f"WebApp API {path} failed: {e}", exc_info=True)
        return _webapp_json({'ok': False, 'error': 'server error'}, 500)


# ── WebApp API per-user rate limiting ─────────────────────────────────
WEBAPP_RATE_LIMIT = 60
WEBAPP_RATE_WINDOW = 60
# Unauthenticated callers only get public endpoints; they are limited by IP
# instead so anonymous traffic cannot be used to hammer this host or the
# upstream price/FX providers.
WEBAPP_ANON_RATE_LIMIT = 30
WEBAPP_RATE_PRUNE_EVERY = 200
_webapp_rate_times: dict[object, list[float]] = defaultdict(list)
_webapp_rate_lock = threading.Lock()
_webapp_rate_calls = 0


def _webapp_rate_limited(key, limit: int = WEBAPP_RATE_LIMIT) -> bool:
    """Return True if the caller (user id or IP key) exceeded the rate limit."""
    global _webapp_rate_calls
    now = time.time()
    with _webapp_rate_lock:
        _webapp_rate_calls += 1
        if _webapp_rate_calls % WEBAPP_RATE_PRUNE_EVERY == 0:
            stale = [k for k, ts in _webapp_rate_times.items()
                     if not ts or now - ts[-1] >= WEBAPP_RATE_WINDOW]
            for k in stale:
                del _webapp_rate_times[k]
        times = _webapp_rate_times[key]
        times = [t for t in times if now - t < WEBAPP_RATE_WINDOW]
        _webapp_rate_times[key] = times
        if len(times) >= limit:
            return True
        times.append(now)
        return False


def _webapp_route(method, path, uid, qs, client_ip=''):
    """Shared router used by both the HTTP server and the WSGI app.
    Returns (status, headers, body_bytes)."""
    if path in ('/api/ping', '/api/ping/'):
        return _webapp_json({'ok': True, 'service': 'earth-crypto-webapp'})
    if path in ('/api/prices', '/api/market', '/api/currency'):
        if uid is not None:
            if _webapp_rate_limited(uid):
                return _webapp_json({'ok': False, 'error': 'rate limited'}, 429)
        elif client_ip and _webapp_rate_limited(('ip', client_ip), WEBAPP_ANON_RATE_LIMIT):
            return _webapp_json({'ok': False, 'error': 'rate limited'}, 429)
        return _webapp_api(path, uid, qs)
    if path.startswith('/api/'):
        if uid is None:
            return _webapp_json({'ok': False, 'error': 'unauthorized'}, 401)
        if _webapp_rate_limited(uid):
            return _webapp_json({'ok': False, 'error': 'rate limited'}, 429)
        return _webapp_api(path, uid, qs)
    if path in ('/', '/index.html'):
        return _webapp_static('index.html')
    return _webapp_static(path.lstrip('/'))


def _webapp_wsgi(environ, start_response):
    """WSGI entry point for cPanel/Passenger (or any WSGI server)."""
    path = unquote(environ.get('PATH_INFO', '/') or '/')
    qs = dict(parse_qsl(environ.get('QUERY_STRING', '')))
    # Security: Only accept initData from header, not query string
    init_data = environ.get('HTTP_X_TELEGRAM_INIT_DATA', '')
    uid = _webapp_validate_init_data(init_data)
    if uid is None and WEBAPP_ALLOW_DEV and qs.get('dev_uid'):
        try:
            uid = int(qs['dev_uid'])
        except (TypeError, ValueError):
            uid = None
    
    # Add CORS headers for development
    method = environ.get('REQUEST_METHOD', 'GET')
    if method == 'OPTIONS':
        start_response("204 No Content", [
            ('Access-Control-Allow-Origin', '*'),
            ('Access-Control-Allow-Methods', 'GET, POST, OPTIONS'),
            ('Access-Control-Allow-Headers', 'Content-Type, X-Telegram-Init-Data'),
            ('Access-Control-Max-Age', '86400'),
        ])
        return [b'']
    
    status, headers, body = _webapp_route(
        method, path, uid, qs, environ.get('REMOTE_ADDR', '') or '')
    
    # Add CORS headers
    headers = list(headers)
    headers.append(('Access-Control-Allow-Origin', '*'))
    headers.append(('Access-Control-Allow-Methods', 'GET, POST, OPTIONS'))
    headers.append(('Access-Control-Allow-Headers', 'Content-Type, X-Telegram-Init-Data'))
    
    # Add CSP header for security
    headers.append(('Content-Security-Policy', WEBAPP_CSP))
    
    start_response(f"{status} {HTTPStatus(status).phrase}", headers)
    return [body]


def start_webapp_server():
    """Start WebApp server with automatic port fallback."""
    max_retries = 3
    base_port = WEBAPP_PORT
    
    for attempt in range(max_retries):
        port = base_port + attempt
        try:
            logger.info(f"Starting WebApp server on port {port} (attempt {attempt + 1}/{max_retries})")
            # create_server() binds the socket HERE, inside this try block, so
            # a busy port raises instead of dying silently inside the thread
            # (waitress.serve() inside a thread made the old except dead code,
            # and the Linux-only errno==98 check also broke on Windows).
            server = waitress.create_server(
                _webapp_wsgi,
                host=WEBAPP_HOST,
                port=port,
                clear_untrusted_proxy_headers=True,
                threads=4
            )
        except OSError as e:
            logger.warning(f"Port {port} not usable ({e}), trying next port...")
            if attempt < max_retries - 1:
                time.sleep(0.5)  # Brief pause before retry
                continue
            logger.error(f"Failed to start WebApp server after {max_retries} attempts")
            raise
        threading.Thread(
            target=server.run,
            daemon=True,
            name='WebAppServer'
        ).start()
        logger.info(f"WebApp server listening on http://{WEBAPP_HOST}:{port} (Waitress WSGI)")
        return None
    logger.error(f"Failed to start WebApp server after {max_retries} attempts")
    return None


# ─────────────────────────────────────────────
# start_bot() - crash recovery polling loop
# Restarts automatically on network errors or
# unexpected crashes, with exponential back-off.
# Used by both `python3 bot.py` and cPanel/Passenger
# (via passenger_wsgi.py).
# ─────────────────────────────────────────────

def _background_scheduler_loop():
    logger.info("Universal background scheduler loop started.")
    last_cache_cleanup = time.time()
    last_alert_check = time.time()
    last_digest_send = time.time()
    last_state_cleanup = time.time()
    last_webapp_cache = time.time()
    last_gc = time.time()

    # Immediate pre-warm
    try:
        _prewarm_prices()
        _spawn_inline_prewarm()
        _prewarm_charts()
    except Exception as e:
        logger.error(f"Initial pre-warm failed: {e}")

    while True:
        now = time.time()
        
        # 1. Alert Checker (every 60s)
        if now - last_alert_check >= 60:
            last_alert_check = now
            try:
                _check_alerts_once()
            except Exception as e:
                logger.error(f"Alert checker loop error: {e}")

        # 2. WebApp Cache Warmer (every 300s)
        # Inline mode only reads cache, so refresh well inside the TTLs
        # (prices 300s, USD/IRR 180s) instead of once per TTL.
        if now - last_webapp_cache >= 90:
            last_webapp_cache = now
            try:
                ids = ','.join(k for k in CRYPTO_LIST if k != 'telegram-stars')
                _fetch_prices_batch(ids)
                _webapp_sparklines(CRYPTO_LIST)
                _spawn_inline_prewarm()
            except Exception as e:
                logger.error(f"WebApp cache warmer failed: {e}")

        # 3. Cache Cleanup (every 300s)
        if now - last_cache_cleanup >= 300:
            last_cache_cleanup = now
            try:
                cache_cleanup()
            except Exception as e:
                pass

        # 4. State Cleanup (every 600s)
        if now - last_state_cleanup >= 600:
            last_state_cleanup = now
            try:
                _cleanup_user_state()
            except Exception as e:
                pass

        # 5. Daily Digest (every 60s check)
        if now - last_digest_send >= 60:
            last_digest_send = now
            try:
                _run_digests_once()
            except Exception as e:
                logger.error(f"Digest scheduler error: {e}")

        # 6. Garbage Collection (every 900s)
        if now - last_gc >= 900:
            last_gc = now
            gc.collect()

        time.sleep(10) # Base tick rate

# Single-instance guards. A second call to start_bot() (double import, two
# "BotMain" threads, a re-spawned scheduler) would run TWO polling loops
# against the same token (Telegram then kicks both with 409 Conflict) plus two
# schedulers that double-send alerts/digests and spam bot.log with duplicate
# lines. These flags make each subsystem idempotent per process.
_scheduler_started = False
_scheduler_lock = threading.Lock()
_webapp_started = False
_webapp_lock = threading.Lock()
_polling_started = False
_polling_lock = threading.Lock()


def _start_scheduler_once():
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        try:
            threading.Thread(target=_background_scheduler_loop, daemon=True, name="Scheduler").start()
        except Exception as e:
            # e.g. RuntimeError: can't start new thread - do not mark as started
            logger.error(f"Failed to start scheduler thread: {e}", exc_info=True)
            return
        _scheduler_started = True


def start_bot(start_web=True):
    # Initialise the animated-emoji mapping (Telethon network call, so do it
    # here at startup rather than at module import).
    try:
        ensure_emoji_map(logger)
    except Exception as e:
        logger.warning(f"Emoji map initialisation failed: {e}")

    # Start background threads (guard: exactly one scheduler per process)
    _start_scheduler_once()
    logger.info(f"{EMOJIS['check']} Universal background scheduler started (replaces 8 legacy threads)")

    if start_web:
        global _webapp_started
        with _webapp_lock:
            if _webapp_started:
                logger.debug("WebApp server already started; skipping")
            else:
                try:
                    start_webapp_server()
                    logger.info(f"{EMOJIS['chart']} Telegram Mini App server ready (webapp/)")
                    _webapp_started = True
                except Exception as e:
                    logger.error(f"WebApp server failed to start: {e}")

    # Guard: only one polling loop may run per process. Without this, two
    # callers would both hit infinity_polling() and Telegram would answer each
    # with "Conflict: terminated by other getUpdates request".
    global _polling_started
    with _polling_lock:
        if _polling_started:
            logger.warning("Polling loop already running in this process; ignoring duplicate start_bot()")
            return
        _polling_started = True

    RETRY_DELAY_MIN = 5    # seconds before first retry
    RETRY_DELAY_MAX = 300  # cap back-off at 5 minutes
    retry_delay = RETRY_DELAY_MIN

    while True:
        try:
            logger.info("Starting polling...")
            bot.infinity_polling(timeout=30, long_polling_timeout=20)
            # infinity_polling only returns on KeyboardInterrupt
            logger.info(f"{EMOJIS['warning']} Polling stopped cleanly.")
            break
        except KeyboardInterrupt:
            logger.info(f"\n{EMOJIS['warning']} Bot stopped by user.")
            break
        except Exception as e:
            logger.error(f"{EMOJIS['cross']} Polling crashed: {e}", exc_info=True)
            logger.info(f"Restarting in {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, RETRY_DELAY_MAX)
        else:
            # Reset back-off on a clean run
            retry_delay = RETRY_DELAY_MIN


if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info(f"{EMOJIS['rocket']} Crypto Price Bot Starting...")
    logger.info(f"{EMOJIS['info']} Bot: @{bot.get_me().username}")
    logger.info(f"{EMOJIS['check']} Status: Running")
    logger.info(f"{EMOJIS['chart']} Cache timeout: {CACHE_TIMEOUT}s")
    logger.info(f"{EMOJIS['info']} Rate limit: {USER_RATE_LIMIT} req / {USER_RATE_WINDOW}s per user")
    logger.info(f"{EMOJIS['info']} Max wallets per user: {MAX_WALLETS_PER_USER}")
    logger.info("Math evaluation: using simpleeval (safe)")
    logger.info(f"{EMOJIS['star']} Press Ctrl+C to stop")
    logger.info("=" * 50)
    start_bot()
