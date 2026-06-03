import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
import telebot
from telebot import types
from telebot.types import InlineKeyboardButton as IKB
import requests
import re
import ast
import time
import logging
import json
import threading
import functools
import sys
import sqlite3
import hashlib
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
from pycoingecko import CoinGeckoAPI
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from io import BytesIO
import concurrent.futures
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
    parse_conversion_command,
    normalize_digits
)

# Animated emoji support (optional — requires Telethon + TG_API_ID/HASH)
from emoji_utils import apply_emoji, ensure_emoji_map

# Safe math evaluation - replaces eval()
try:
    from simpleeval import simple_eval
    SAFE_EVAL_AVAILABLE = True
except ImportError:
    SAFE_EVAL_AVAILABLE = False
    logging.warning("simpleeval not installed. Install with: pip install simpleeval")

cg = CoinGeckoAPI()
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
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
            if time.time() - ts < 3600:
                return val
    try:
        chat_id = REQUIRED_CHANNEL
        member = bot.get_chat_member(chat_id, user_id)
        result = member.status in ('member', 'administrator', 'creator')
    except Exception as e:
        logger.warning(f"Failed to check channel membership for user {user_id}: {e}")
        result = False
    with _joined_cache_lock:
        _joined_cache[user_id] = (result, time.time())
    return result


def _send_join_required(chat_id: int):
    """Send a message asking the user to join the required channel."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 Join Channel", url=REQUIRED_CHANNEL))
    kb.add(types.InlineKeyboardButton("✅ I've Joined", callback_data="check_join"))
    bot.send_message(
        chat_id,
        f"📢 <b>Please join our channel first!</b>\n\n"
        f"You need to join the channel below to use this bot:\n{REQUIRED_CHANNEL}\n\n"
        f"After joining, click \"I've Joined\" to continue.",
        parse_mode='HTML',
        reply_markup=kb
    )


# ── Animated emoji setup ──────────────────────────────────────────────
ensure_emoji_map(logger)

_TG_EMOJI_RE = re.compile(r'<tg-emoji[^>]*>|</tg-emoji>')

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
        except Exception:
            pass
    return _orig_send_photo(chat_id, photo, **kwargs)

def _emoji_edit_message_caption(**kwargs):
    caption = kwargs.get('caption')
    if caption and kwargs.get('parse_mode') == 'HTML':
        emoji_caption = apply_emoji(caption)
        try:
            return _orig_edit_message_caption(**{**kwargs, 'caption': emoji_caption})
        except Exception:
            pass
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

# ─────────────────────────────────────────────
# SQLite persistence (replaces JSON files)
# ─────────────────────────────────────────────
DB_FILE = "bot_data.db"
db_lock = threading.Lock()


# Track last rate limit notification per user
_rate_limit_notified = {}
_rate_limit_notified_lock = threading.Lock()
RATE_LIMIT_NOTIFY_COOLDOWN = 60  # Only notify once per minute


def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
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
        conn.commit()
        conn.close()
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
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT lang FROM user_languages WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()
    lang = row[0] if row else 'en'
    with _lang_cache_lock:
        _lang_cache[user_id] = lang
    return lang


def _get_lang_cached(user_id: int) -> str:
    """Read language from cache only — no DB lock. For background threads."""
    with _lang_cache_lock:
        return _lang_cache.get(user_id, 'en')


def db_set_lang(user_id: int, lang: str) -> None:
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO user_languages (user_id, lang) VALUES (?,?)",
            (user_id, lang)
        )
        conn.commit()
        conn.close()
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
    """Like T() but uses cached language only — no DB lock. For background threads."""
    lang = _get_lang_cached(user_id)
    text = STRINGS.get(lang, STRINGS['en']).get(key)
    if text is None:
        text = STRINGS['en'].get(key, f'[{key}]')
    return text.format(**kwargs) if kwargs else text


def db_get_holdings(user_id):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT data FROM holdings WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()
    return json.loads(row[0]) if row else None


def db_set_holdings(user_id, holdings_dict):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO holdings (user_id, data) VALUES (?,?)",
            (user_id, json.dumps(holdings_dict))
        )
        conn.commit()
        conn.close()


def db_get_wallets(user_id):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT address FROM wallets WHERE user_id=?", (user_id,))
        rows = c.fetchall()
        conn.close()
    return [r[0] for r in rows]


def db_add_wallet(user_id, address):
    """Returns False if already exists, True on success."""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO wallets (user_id, address) VALUES (?,?)", (user_id, address))
            conn.commit()
            success = True
        except sqlite3.IntegrityError:
            success = False
        conn.close()
    return success


def db_clear_wallets(user_id):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM wallets WHERE user_id=?", (user_id,))
        affected = c.rowcount
        conn.commit()
        conn.close()
    return affected > 0


def db_remove_wallet(user_id, address):
    """Remove a single wallet. Returns True if it existed."""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM wallets WHERE user_id=? AND address=?", (user_id, address))
        affected = c.rowcount
        conn.commit()
        conn.close()
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
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
    return True


# ── Alerts ────────────────────────────────────
def db_add_alert(user_id, crypto_id, symbol, target_price, direction):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT INTO alerts (user_id, crypto_id, symbol, target_price, direction, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, crypto_id, symbol.upper(), target_price, direction, time.time())
        )
        alert_id = c.lastrowid
        conn.commit()
        conn.close()
    return alert_id


def db_get_alerts(user_id):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "SELECT id, crypto_id, symbol, target_price, direction FROM alerts WHERE user_id=? ORDER BY id",
            (user_id,)
        )
        rows = c.fetchall()
        conn.close()
    return [{'id': r[0], 'crypto_id': r[1], 'symbol': r[2],
             'target_price': r[3], 'direction': r[4]} for r in rows]


def db_get_all_alerts():
    """Used by the alert checker thread."""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id, user_id, crypto_id, symbol, target_price, direction FROM alerts")
        rows = c.fetchall()
        conn.close()
    return [{'id': r[0], 'user_id': r[1], 'crypto_id': r[2],
             'symbol': r[3], 'target_price': r[4], 'direction': r[5]} for r in rows]


def db_delete_alert(alert_id, user_id):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM alerts WHERE id=? AND user_id=?", (alert_id, user_id))
        affected = c.rowcount
        conn.commit()
        conn.close()
    return affected > 0


def db_delete_alert_by_id(alert_id):
    """Used internally by the alert firing thread."""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
        conn.commit()
        conn.close()


# ── Buy prices (P&L) ──────────────────────────
def db_set_buy_price(user_id, symbol, buy_price):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO buy_prices (user_id, symbol, buy_price) VALUES (?,?,?)",
            (user_id, symbol.upper(), buy_price)
        )
        conn.commit()
        conn.close()


def db_get_buy_prices(user_id):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT symbol, buy_price FROM buy_prices WHERE user_id=?", (user_id,))
        rows = c.fetchall()
        conn.close()
    return {r[0]: r[1] for r in rows}


def db_delete_buy_price(user_id, symbol):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM buy_prices WHERE user_id=? AND symbol=?", (user_id, symbol.upper()))
        conn.commit()
        conn.close()


# ── Daily digest ──────────────────────────────
def db_set_digest(user_id, enabled: bool, hour: int = 9):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO digest_prefs (user_id, enabled, hour) VALUES (?,?,?)",
            (user_id, int(enabled), hour)
        )
        conn.commit()
        conn.close()


def db_get_digest(user_id):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT enabled, hour FROM digest_prefs WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()
    return {'enabled': bool(row[0]), 'hour': row[1]} if row else None


def db_get_all_digest_users():
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id, hour FROM digest_prefs WHERE enabled=1")
        rows = c.fetchall()
        conn.close()
    return [{'user_id': r[0], 'hour': r[1]} for r in rows]


init_db()

# ─────────────────────────────────────────────
# Helper Functions - Timestamps, Security, Monitoring
# ─────────────────────────────────────────────

def get_iran_time_str():
    """Get current date and time in Iran timezone as formatted string."""
    now = datetime.now(IRAN_TZ)
    return now.strftime('%Y-%m-%d %H:%M')  # Format: 2026-04-17 14:30

def add_timestamp(text: str) -> str:
    """Add Iran local date/time timestamp to message."""
    time_str = get_iran_time_str()
    return f"{text}\n\n🕐 {time_str} (Iran)"


def _sym(cid: str) -> str:
    """Extract ticker: '🪙 Bitcoin (BTC)' → 'BTC'"""
    entry = CRYPTO_LIST.get(cid)
    if entry and '(' in entry:
        return entry.split('(')[1].replace(')', '').strip()
    return cid.upper()


def register_panel_owner(message_id: int, user_id: int):
    """Register who owns a button panel."""
    with panel_owners_lock:
        panel_owners[message_id] = {
            'user_id': user_id,
            'expires_at': time.time() + PANEL_TIMEOUT
        }

def check_panel_owner(message_id: int, user_id: int) -> bool:
    """Check if user owns the panel. Returns True if allowed."""
    with panel_owners_lock:
        if message_id not in panel_owners:
            return True  # No restriction if not registered
        
        panel = panel_owners[message_id]
        
        # Check if expired
        if time.time() > panel['expires_at']:
            del panel_owners[message_id]
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
                return random.choice(funny_messages)
    
    return None

# ─────────────────────────────────────────────
# Thread-safe in-memory cache
# ─────────────────────────────────────────────
_cache = {}
_cache_lock = threading.Lock()
CACHE_TIMEOUT = 120  # 2 minute default cache for price data


def cache_get(key):
    """Get cached value, returns None if missing or expired. Cleans up stale entries."""
    with _cache_lock:
        if key not in _cache:
            return None
        value, timestamp, ttl = _cache[key]
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


def _cleanup_user_state_loop():
    """Periodically purge expired user state entries (30 min TTL)."""
    while True:
        time.sleep(120)
        try:
            now = time.time()
            with _user_state_lock:
                expired = [uid for uid, expiry in _user_state_ttl.items() if now > expiry]
                for uid in expired:
                    user_state.pop(uid, None)
                    _user_state_ttl.pop(uid, None)
                if expired:
                    logger.debug(f"Cleaned {len(expired)} stale user state entries")
        except Exception as e:
            logger.error(f"User state cleanup error: {e}")


# ─────────────────────────────────────────────
# Thread-safe rate limiter
# ─────────────────────────────────────────────
_api_lock = threading.Lock()
_last_api_call = 0.0
API_COOLDOWN = 1.2


def rate_limited_api_call(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _last_api_call
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
    for attempt in range(2):
        try:
            resp = requests.get(
                f"https://api.coingecko.com/api/v3/simple/price"
                f"?ids={ids}&vs_currencies=usd&include_24hr_change=true",
                timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                time.sleep(2)
        except Exception as e:
            logger.error(f"CoinGecko batch error: {e}")
            time.sleep(1)
    return {}


_COINCAP_ID_MAP = {
    'binancecoin': 'binance-coin', 'the-open-network': 'toncoin',
}


def _fetch_prices_coincap(ids) -> dict:
    try:
        id_list = ids.split(',')
        cap_ids = ','.join(_COINCAP_ID_MAP.get(i, i) for i in id_list)
        resp = requests.get(f"https://api.coincap.io/v2/assets?ids={cap_ids}", timeout=10)
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


def _fetch_prices_binance(ids) -> dict:
    try:
        id_list = ids.split(',')
        symbols = [f"{EXCHANGE_SYMBOL_MAP.get(c, c).upper()}USDT" for c in id_list
                   if c in EXCHANGE_SYMBOL_MAP]
        if not symbols:
            return {}
        resp = requests.get(
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
            cache_set(cache_key, data, ttl=30)
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
    Cleans up old timestamps on each call.
    """
    now = time.time()
    with _user_rate_lock:
        times = _user_request_times[user_id]
        # Drop timestamps outside the window
        _user_request_times[user_id] = [t for t in times if now - t < USER_RATE_WINDOW]
        if len(_user_request_times[user_id]) >= USER_RATE_LIMIT:
            return True
        _user_request_times[user_id].append(now)
        return False


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


def rate_limit_check(func):
    """Decorator — drops rate-limited updates and notifies user once per minute.
    Only counts bot commands (messages starting with /) toward the rate limit.
    Also monitors group command frequency for slowdown warnings.
    """
    @functools.wraps(func)
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id

        is_command = (hasattr(message, 'text') and message.text
                      and message.text.startswith('/'))
        # Only rate-limit bot commands, not regular chat messages
        if is_command and is_user_rate_limited(user_id):
            logger.warning(f"User {user_id} rate-limited - request dropped")
            
            # Only notify once per minute to avoid spam
            current_time = time.time()
            with _rate_limit_notified_lock:
                last_notified = _rate_limit_notified.get(user_id, 0)
                if current_time - last_notified <= RATE_LIMIT_NOTIFY_COOLDOWN:
                    return
                _rate_limit_notified[user_id] = current_time
            try:
                lang = db_get_lang(user_id)
                if lang == 'fa':
                    msg = "⏳ <b>آروم‌تر!</b>\n\nلطفاً یک لحظه صبر کنید."
                else:
                    msg = "⏳ <b>Slow down!</b>\n\nYou're sending messages too quickly. Please wait a moment before trying again."
                bot.reply_to(message, msg, parse_mode='HTML')
            except:
                pass  # If notification fails, just drop silently
            
            return

        # ⭐ Monitor group command rate for slowdown warnings (bot commands only)
        if is_command and message.chat.type in ['group', 'supergroup']:
            warning = monitor_group_activity(message.chat.id, time.time())
            if warning:
                try:
                    bot.send_message(message.chat.id, warning)
                except:
                    pass

        return func(message, *args, **kwargs)
    return wrapper



# ─────────────────────────────────────────────
# i18n string table  (en + fa)
# Add new strings here — never hardcode in handlers
# ─────────────────────────────────────────────
STRINGS = {
    'en': {
        # ── Generic ───────────────────────────────────────────
        'cancelled':           "✅ Cancelled.",
        'nothing_to_cancel':   "ℹ️ Nothing to cancel.",
        'something_went_wrong': "❌ Something went wrong. Please try again.",
        'slow_down':           "⏳ Slow down a little! Try again in a moment.",
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

        # ── Language selection ────────────────────────────────
        'lang_prompt':         "🌐 <b>Choose your language</b>\nزبان خود را انتخاب کنید:",
        'lang_set_en':         "✅ Language set to <b>English</b>.",
        'lang_set_fa':         "✅ زبان به <b>فارسی</b> تغییر یافت.",

        # ── /start ────────────────────────────────────────────
        'start_welcome':
            "🌍 <b>Welcome to Earth Crypto, {name}! 🚀</b>\n"
            "<i>Prices · Charts · Portfolio · Alerts · Wallets · NFTs</i>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📈 <b>Market</b>\n"
            "  /price — All coin prices\n"
            "  /chart — Price chart (e.g. /chart btc 90d)\n"
            "  /compare — Compare two coins\n"
            "  /market — Fear &amp; Greed overview\n"
            "  /trending — Top trending coins\n"
            "  /gainers — Top gainers (24h)\n"
            "  /losers — Top losers (24h)\n"
            "  /gold — Gold prices (global)\n\n"
            "💱 <b>Currency</b>\n"
            "  /usd — USD ↔ Toman\n"
            "  /try — TRY ↔ Toman\n"
            "  /convert — Coins · USD · Toman\n"
            "  /star — Telegram Stars\n"
            "  /setexchange — Price source (Binance/Crypto.com/Kraken)\n\n"
            "💼 <b>Portfolio</b>\n"
            "  /holdings — Live P&amp;L snapshot\n"
            "  /set — Update your holdings\n"
            "  /digest — Daily summary\n\n"
            "🔔 <b>Alerts</b>\n"
            "  /alert — New price alert\n"
            "  /alerts — Manage alerts\n\n"
            "👛 <b>Wallet Checker</b>\n"
            "  /wallets — Manage TRON addresses\n"
            "  /mywallets — Live TRX balances\n"
            "  Send TRON/TON address → balance\n"
            "  Send TRX/TON tx hash → details\n\n"
            "🏷 <b>Telegram NFTs (Fragment)</b>\n"
            "  /fragment — Lookup a username\n"
            "  /gifts — Gift collections\n"
            "  /gift — Collection details\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ <b>Just type — no command needed:</b>\n"
            "  <code>btc</code>  →  price + chart\n"
            "  <code>btc @binance</code>  →  price from Binance\n"
            "  <code>10 trx</code>  →  USD + Toman value\n"
            "  <code>0.01 btc to eth</code>  →  conversion\n"
            "  <code>150 usd</code>  →  USD → Toman\n"
            "  <code>10+20*3</code>  →  calculator\n"
            "  <code>15% of 200</code>  →  percentage\n"
            "  TRON/TON address  →  balance\n"
            "  tx hash / link  →  details\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 /suggest  ·  💖 /donate  ·  🔒 /privacy  ·  🌐 /language\n\n"
            "Use <code>@EscEarthBot</code> in any chat for inline prices. 🚀",

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
            "• <b>Holdings</b> — coin amounts you set via /set or /holdings\n"
            "• <b>Buy prices</b> — average buy prices for P&amp;L tracking\n"
            "• <b>Wallets</b> — TRON wallet addresses you add via /wallets\n"
            "• <b>Alerts</b> — price alert targets you set via /alert\n"
            "• <b>Digest preference</b> — your daily digest on/off setting and time\n"
            "• <b>Language</b> — your chosen interface language\n\n"
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
        'wallet_invalid':      "❌ <b>Invalid address</b>\n\n<code>{address}</code>\n\nA TRON address starts with <b>T</b> and is exactly <b>34 characters</b> long. Please double-check and try again.",
        'wallet_already_saved':"⚠️ This wallet is already saved.",
        'wallet_not_found':    "Wallet not found — list may have changed.",
        'wallet_removed_toast':"✅ Wallet removed.",
        'no_wallets_yet':      "⚠️ No saved wallets yet.\nUse /wallets to manage them.",
        'wallets_balances_hdr':"👛 <b>Wallets &amp; Balances</b>\n\n",
        'btn_view_wallets':    "👁 View Wallets",
        'btn_add_wallet':      "➕ Add Wallet",
        'btn_close':           "❌ Close",
        'btn_remove':          "🗑 Remove",
        'send_wallet_addr':    "🔗 Send your TRON wallet address:\n(or /cancel to abort)",
        'no_balance':          "❌ No balance found for this address.",
        'trx_balance':         "👛 TRX Balance: {bal} TRX",
        'tron_timeout':        "⏳ Request timed out. Try again.",
        'tron_error':          "❌ Could not reach TRON network. Try again.",
        'invalid_tron_addr':   "❌ That doesn\'t look like a valid TRON address. Please check it and try again.",

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
        'usd_rate':            "💵 <b>1 USD = {rate} Toman</b>",
        'usd_conversion':      "💵 <b>{amount} USD = {result} Toman</b>",
        'rate_unavailable':    "⚠️ USD/IRR rate unavailable. Please try again later.",

        # ── /try (Turkish Lira) ───────────────────────────────
        'try_rate':            "🇹🇷 <b>1 TRY = {rate} Toman</b>",

        # ── /gold ─────────────────────────────────────────────
        'gold_global':         "🌍 <b>Global Gold</b>\nXAU/USD: ${xau}/oz",
        'gold_iran':           "\n\n🇮🇷 <b>Iran Gold Coins</b> (Toman)\n",
        'gold_bahar':          "🪙 Bahar Azadi: {price}\n",
        'gold_emami':          "🪙 Emami: {price}\n",
        'gold_nim':            "🪙 Nim (½): {price}\n",
        'gold_rob':            "🪙 Rob (¼): {price}\n",
        'gold_gram18':         "⚖️ 18K Gold/g: {price}",
        'gold_fetch_fail':     "❌ Could not fetch gold prices. Try again.",

        # ── /market ───────────────────────────────────────────
        'market_header':       "🌍 <b>Crypto Market</b>\n\n",
        'market_mcap':         "💹 Market Cap: <b>{mcap}</b>  {arrow} {chg}% (24h)\n",
        'market_vol':          "📊 24h Volume: <b>{vol}</b>\n",
        'market_dom':          "🟠 BTC Dom: <b>{btc}%</b>  🔵 ETH Dom: <b>{eth}%</b>\n",
        'market_coins':        "🪙 Active coins: <b>{coins}</b>\n\n",
        'market_fg':           "😨 <b>Fear &amp; Greed</b>\n{bar}",
        'fg_unavailable':      "Unavailable",
        'fg_extreme_fear':     "Extreme Fear",
        'fg_fear':             "Fear",
        'fg_neutral':          "Neutral",
        'fg_greed':            "Greed",
        'fg_extreme_greed':    "Extreme Greed",

        # ── /compare ──────────────────────────────────────────
        'compare_header':      "📊 <b>Comparison</b>\n\n",
        'compare_pick1':       "📊 <b>Compare Coins</b>\n\nPick the <b>first</b> coin:",
        'compare_pick2':       "📊 <b>Compare Coins</b>\n\n✅ First: <b>{sym}</b>\n\nNow pick the <b>second</b> coin:",
        'compare_vol':         "📦 Vol 24h: {vol}\n",
        'compare_mcap':        "🏦 MCap: {mcap}",
        'compare_winner':      "\n🏆 <b>{name}</b> performed better in the last 24h",
        'compare_tied':        "\n🤝 <b>Tied</b> — same 24h performance",

        # ── /convert ──────────────────────────────────────────
        'convert_step1':       "💱 <b>Convert</b>\n\nStep 1 of 3 — Pick the coin to convert <b>from</b>:",
        'convert_step2':       "💱 <b>Convert</b>\n\n✅ From: <b>{sym}</b>\n\nStep 2 of 3 — Pick the coin to convert <b>to</b>:",
        'convert_step3':       "💱 <b>Convert</b>\n\n✅ From: <b>{from_sym}</b>\n✅ To: <b>{to_sym}</b>\n\nStep 3 of 3 — How much <b>{from_sym}</b>?\n<i>e.g. 10</i>\n\nType /cancel to abort.",
        'convert_result':      "💱 <b>{amount} {from_sym}</b>  →  <b>{result} {to_sym}</b>",
        'convert_fail':        "❌ Conversion failed: {err}",
        'inline_conv_header':  "💱 Conversion",
        'btn_cvt_cancel':      "❌ Cancel",

        # ── /alert ────────────────────────────────────────────
        'alert_step1':         "🔔 <b>Set Price Alert</b>\n\nStep 1 of 3 — Pick a coin:",
        'alert_step2':         "🔔 <b>Set Price Alert</b>\n\n✅ Coin: <b>{sym}</b>  (now {price})\n\nStep 2 of 3 — Notify me when price:",
        'alert_step3':         "🔔 <b>Set Price Alert</b>\n\n✅ Coin: <b>{sym}</b>  (now {price})\n✅ Direction: {arrow} <b>{direction}</b>\n\nStep 3 of 3 — Send the target price:\n<i>e.g. 95000</i>\n\nType /cancel to abort.",
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
            "To cancel, send /cancel",
        'digest_time_confirm': "✅ Digest time set to <b>{hour}:00</b>.",
        'digest_morning':      "☀️ <b>Good morning! Here\'s your portfolio</b>\n",
        'digest_total':        "\n💰 <b>Total: {usd}</b> · {irr} Toman",
        'btn_portfolio':       "💼 Portfolio",
        'btn_alerts':          "🔔 Alerts",

        # ── TRON transaction ──────────────────────────────────
        'tx_header':           "ℹ️ TRON Transaction Details\n\n",
        'tx_status':           "{emoji} Status: {status}\n\n",
        'tx_confirmed':        "Confirmed",
        'tx_pending':          "Pending",
        'tx_block':            "🔗 Block: #{block}\n\n",
        'tx_time':             "🕐 Time: {time}\n\n",
        'tx_from':             "📤 From:\n{addr}\n\n",
        'tx_to':               "📥 To:\n{addr}\n\n",
        'tx_amount':           "💰 Amount: {amount} TRX\n\n",
        'tx_fee':              "⛽ Network Fee: {fee} TRX\n\n",
        'tx_hash':             "📝 TX Hash:\n{hash}\n",
        'tx_not_found':        "❌ Transaction not found. Check the hash and try again.",
        'tx_timeout':          "⏳ Request timed out. Try again.",
        'tx_error':            "❌ Could not fetch transaction. Check the hash and try again.",

        # ── inline / misc ─────────────────────────────────────
        'inline_tips_title':   "Crypto Bot — Quick Tips",
        'inline_tips_desc':    "btc · 10trx · 100usd to eth · u · wallets · hash…",
        'inline_tips_body':
            "<b>Inline Mode Tips:</b>\n\n"
            "• <code>btc</code> — price\n"
            "• <code>10trx</code> or <code>10 trx</code> — value\n"
            "• <code>u</code> or <code>10u</code> — USDT\n"
            "• <code>100usd to eth</code> — convert\n"
            "• <code>usd</code> — dollar rate\n"
            "• <code>price</code> — all prices\n"
            "• <code>wallets</code> — with balance  |  <code>wallets addr</code> — address only\n"
            "• <code>10+20*3</code> — calculator\n"
            "• <code>15% of 200</code>  |  <code>100+5%</code>  |  <code>80-20%</code> — percentage\n"
            "• Paste TRON tx hash or Tronscan link — details",
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
        'gdpr_cancelled':      "Cancelled — your data is safe.",

        # ── Currency / price display ──────────────────────────
        'toman_label':         "Toman",
        'convert_toman_note':  "\n💰 {irr} Toman",
        'convert_toman_approx':"\n💰 ≈ {usd} · {irr} Toman",
        'price_toman_line':    "💰 {irr} Toman\n",
        'inline_usd_toman':    "USD → Toman",
        'inline_toman_usd':    "Toman → USD",
        'usd_simple':          "1 USD = {rate} Toman",
        'usd_amount':          "{amount} USD = {rate} Toman",

        # ── Exchange / Chart / Trending / Fragment ─────────────
        'exch_set':            "✅ Default price source set to <b>{name}</b>",
        'exch_unknown':        "❌ Unknown exchange. Choose: {list}",
        'exch_usage':          "Usage: /setexchange binance\nOr add @exchange to any query: btc @binance",
        'exch_prompt':         "💱 <b>Select your default price source:</b>",
        'chart_usage':         "Usage:\n/chart btc\n/chart btc 7d\n/chart btc 90d",
        'chart_fail':          "❌ Chart generation failed. Try again later.",
        'compare_usage':       "Usage: /compare btc eth\n/compare btc sol 90d",
        'compare_fail':        "❌ Comparison failed. Try again later.",
        'trending_header':     "🔥 <b>Trending on CoinGecko</b>\n",
        'trending_fail':       "❌ Could not fetch trending data. Try again later.",
        'gainers_header':      "📈 <b>Top Gainers (24h)</b>\n",
        'losers_header':       "📉 <b>Top Losers (24h)</b>\n",
        'movers_fail':         "❌ Could not fetch market data. Try again later.",
        'fragment_usage':      "Usage: /fragment @username\nLook up a Telegram username on Fragment.com",
        'fragment_looking':    "🔍 Looking up <b>{target}</b> on Fragment...",
        'fragment_fail':       "❌ Could not fetch data from Fragment. The username may not exist.",
        'fragment_owned':      "Owned / Not for sale",
        'fragment_for_sale':   "For sale",
        'fragment_auction':    "On auction",
        'fragment_status':     "📌 Status: <b>{status}</b>",
        'fragment_price':      "💰 Price: <b>{ton} TON</b>{usd}",
        'fragment_owner':      "👤 Owner: <code>{wallet}</code>",
        'gifts_header':        "🎁 <b>Telegram Gift Collections</b>\n",
        'gifts_fail':          "❌ Could not fetch gift collections.",
        'gifts_usage':         "Usage: /gift collection_slug\nExample: /gift astralshard",
        'gift_not_found':      "❌ Collection <b>{slug}</b> not found. Use /gifts to see all.",
    },

    'fa': {
        # ── Generic ───────────────────────────────────────────
        'cancelled':           "✅ لغو شد.",
        'nothing_to_cancel':   "ℹ️ چیزی برای لغو وجود ندارد.",
        'something_went_wrong':"❌ مشکلی پیش آمد. لطفاً دوباره تلاش کنید.",
        'slow_down':           "⏳ کمی آهسته‌تر! لطفاً یک لحظه صبر کنید.",
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

        # ── Language selection ────────────────────────────────
        'lang_prompt':         "🌐 <b>Choose your language</b>\nزبان خود را انتخاب کنید:",
        'lang_set_en':         "✅ Language set to <b>English</b>.",
        'lang_set_fa':         "✅ زبان به <b>فارسی</b> تغییر یافت.",

        # ── /start ────────────────────────────────────────────
        'start_welcome':
            "🌍 <b>سلام {name}، به Earth Crypto خوش آمدید! 🚀</b>\n"
            "<i>قیمت · نمودار · پرتفو · هشدار · کیف پول · NFT</i>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📈 <b>بازار</b>\n"
            "  /price — قیمت لحظه‌ای ارزها\n"
            "  /chart — نمودار قیمت (مثال: /chart btc 90d)\n"
            "  /compare — مقایسه دو ارز\n"
            "  /market — شاخص ترس و طمع\n"
            "  /trending — محبوب‌ترین ارزها\n"
            "  /gainers — پربازده‌ترین‌ها (24h)\n"
            "  /losers — پرضررترین‌ها (24h)\n"
            "  /gold — قیمت طلا\n\n"
            "💱 <b>ارز</b>\n"
            "  /usd — دلار ↔ تومان\n"
            "  /try — لیر ترکیه ↔ تومان\n"
            "  /convert — ارز · دلار · تومان\n"
            "  /star — Telegram Stars\n"
            "  /setexchange — منبع قیمت (Binance/Crypto.com/Kraken)\n\n"
            "💼 <b>پرتفو</b>\n"
            "  /holdings — سود/زیان لحظه‌ای\n"
            "  /set — ثبت دارایی‌ها\n"
            "  /digest — خلاصه روزانه\n\n"
            "🔔 <b>هشدار قیمت</b>\n"
            "  /alert — هشدار جدید\n"
            "  /alerts — مدیریت هشدارها\n\n"
            "👛 <b>بررسی کیف پول</b>\n"
            "  /wallets — مدیریت آدرس‌های TRON\n"
            "  /mywallets — موجودی TRX\n"
            "  آدرس TRON/TON → موجودی\n"
            "  هش تراکنش TRX/TON → جزئیات\n\n"
            "🏷 <b>NFT تلگرام (Fragment)</b>\n"
            "  /fragment — بررسی نام کاربری\n"
            "  /gifts — مجموعه هدایا\n"
            "  /gift — جزئیات مجموعه\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ <b>فقط تایپ کنید:</b>\n"
            "  <code>بیتکوین</code>  ←  قیمت + نمودار\n"
            "  <code>بیتکوین @binance</code>  ←  قیمت از Binance\n"
            "  <code>10 ترون</code>  ←  ارزش به تومان\n"
            "  <code>0.01 بیت به اتریوم</code>  ←  تبدیل\n"
            "  <code>150 دلار</code>  ←  دلار به تومان\n"
            "  <code>10+20*3</code>  ←  ماشین‌حساب\n"
            "  <code>15% از 200</code>  ←  درصد\n"
            "  آدرس TRON/TON → موجودی\n"
            "  هش/لینک تراکنش → جزئیات\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 /suggest  ·  💖 /donate  ·  🔒 /privacy  ·  🌐 /language\n\n"
            "در هر چتی <code>@EscEarthBot</code> را تایپ کنید. 🚀",

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
            "• <b>دارایی‌ها</b> — مقادیر ارز که از طریق /set یا /holdings وارد کردید\n"
            "• <b>قیمت خرید</b> — میانگین قیمت خرید برای محاسبه سود/زیان\n"
            "• <b>کیف پول‌ها</b> — آدرس‌های ترون که اضافه کردید\n"
            "• <b>هشدارها</b> — اهداف قیمتی که تنظیم کردید\n"
            "• <b>تنظیمات خلاصه</b> — وضعیت و زمان خلاصه روزانه\n"
            "• <b>زبان</b> — زبان انتخابی شما\n\n"
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
            "• تنظیمات زبان\n\n"
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
        'wallet_invalid':      "❌ <b>آدرس نامعتبر</b>\n\n<code>{address}</code>\n\nآدرس ترون با <b>T</b> شروع می‌شود و دقیقاً <b>۳۴ کاراکتر</b> دارد. لطفاً دوباره بررسی کنید.",
        'wallet_already_saved':"⚠️ این کیف پول قبلاً ذخیره شده.",
        'wallet_not_found':    "کیف پول پیدا نشد — لیست ممکن است تغییر کرده باشد.",
        'wallet_removed_toast':"✅ کیف پول حذف شد.",
        'no_wallets_yet':      "⚠️ هنوز کیف پولی ذخیره نشده.\nاز /wallets برای مدیریت استفاده کنید.",
        'wallets_balances_hdr':"👛 <b>کیف پول‌ها و موجودی</b>\n\n",
        'btn_view_wallets':    "👁 مشاهده کیف پول‌ها",
        'btn_add_wallet':      "➕ افزودن کیف پول",
        'btn_close':           "❌ بستن",
        'btn_remove':          "🗑 حذف",
        'send_wallet_addr':    "🔗 آدرس کیف پول ترون خود را بفرستید:\n(یا /cancel برای لغو)",
        'no_balance':          "❌ موجودی برای این آدرس یافت نشد.",
        'trx_balance':         "👛 موجودی TRX: {bal} TRX",
        'tron_timeout':        "⏳ درخواست منقضی شد. دوباره امتحان کنید.",
        'tron_error':          "❌ اتصال به شبکه ترون ممکن نیست. دوباره امتحان کنید.",
        'invalid_tron_addr':   "❌ این آدرس ترون معتبر نیست. لطفاً بررسی کنید و دوباره امتحان کنید.",

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
        'usd_rate':            "💵 <b>1 USD = {rate} Toman</b>",
        'usd_conversion':      "💵 <b>{amount} دلار = {result} تومان</b>",
        'rate_unavailable':    "⚠️ نرخ دلار در دسترس نیست. بعداً امتحان کنید.",

        # ── /try (لیر ترکیه) ──────────────────────────────────
        'try_rate':            "🇹🇷 <b>1 TRY = {rate} Toman</b>",

        # ── /gold (طلا) ───────────────────────────────────────
        'gold_global':         "🌍 <b>طلای جهانی</b>\nXAU/USD: ${xau}/oz",
        'gold_iran':           "\n\n🇮🇷 <b>سکه‌های ایران</b> (تومان)\n",
        'gold_bahar':          "🪙 بهار آزادی: {price}\n",
        'gold_emami':          "🪙 امامی: {price}\n",
        'gold_nim':            "🪙 نیم: {price}\n",
        'gold_rob':            "🪙 ربع: {price}\n",
        'gold_gram18':         "⚖️ گرم 18: {price}",
        'gold_fetch_fail':     "❌ قیمت طلا دریافت نشد. دوباره تلاش کنید.",

        # ── /market ───────────────────────────────────────────
        'market_header':       "🌍 <b>بازار کریپتو</b>\n\n",
        'market_mcap':         "💹 ارزش بازار: <b>{mcap}</b>  {arrow} {chg}% (۲۴ ساعت)\n",
        'market_vol':          "📊 حجم ۲۴ ساعته: <b>{vol}</b>\n",
        'market_dom':          "🟠 سهم BTC: <b>{btc}%</b>  🔵 سهم ETH: <b>{eth}%</b>\n",
        'market_coins':        "🪙 ارزهای فعال: <b>{coins}</b>\n\n",
        'market_fg':           "😨 <b>شاخص ترس و طمع</b>\n{bar}",
        'fg_unavailable':      "در دسترس نیست",
        'fg_extreme_fear':     "ترس شدید",
        'fg_fear':             "ترس",
        'fg_neutral':          "خنثی",
        'fg_greed':            "طمع",
        'fg_extreme_greed':    "طمع شدید",

        # ── /compare ──────────────────────────────────────────
        'compare_header':      "📊 <b>مقایسه</b>\n\n",
        'compare_pick1':       "📊 <b>مقایسه ارزها</b>\n\nارز <b>اول</b> را انتخاب کنید:",
        'compare_pick2':       "📊 <b>مقایسه ارزها</b>\n\n✅ اول: <b>{sym}</b>\n\nحالا ارز <b>دوم</b> را انتخاب کنید:",
        'compare_vol':         "📦 Vol 24h: {vol}\n",
        'compare_mcap':        "🏦 ارزش بازار: {mcap}",
        'compare_winner':      "\n🏆 <b>{name}</b> در ۲۴ ساعت گذشته بهتر عمل کرد",
        'compare_tied':        "\n🤝 <b>مساوی</b> — عملکرد یکسان در ۲۴ ساعت",

        # ── /convert ──────────────────────────────────────────
        'convert_step1':       "💱 <b>تبدیل ارز</b>\n\nمرحله ۱ از ۳ — ارز مبدا را انتخاب کنید:",
        'convert_step2':       "💱 <b>تبدیل ارز</b>\n\n✅ از: <b>{sym}</b>\n\nمرحله ۲ از ۳ — ارز مقصد را انتخاب کنید:",
        'convert_step3':       "💱 <b>تبدیل ارز</b>\n\n✅ از: <b>{from_sym}</b>\n✅ به: <b>{to_sym}</b>\n\nمرحله ۳ از ۳ — چقدر <b>{from_sym}</b>؟\n<i>مثال: 10</i>\n\nبرای لغو /cancel بزنید.",
        'convert_result':      "💱 <b>{amount} {from_sym}</b>  →  <b>{result} {to_sym}</b>",
        'convert_fail':        "❌ خطا در تبدیل: {err}",
        'inline_conv_header':  "💱 تبدیل ارز",
        'btn_cvt_cancel':      "❌ لغو",

        # ── /alert ────────────────────────────────────────────
        'alert_step1':         "🔔 <b>تنظیم هشدار قیمت</b>\n\nمرحله ۱ از ۳ — ارز را انتخاب کنید:",
        'alert_step2':         "🔔 <b>تنظیم هشدار قیمت</b>\n\n✅ ارز: <b>{sym}</b>  (اکنون {price})\n\nمرحله ۲ از ۳ — هشدار بده وقتی قیمت:",
        'alert_step3':         "🔔 <b>تنظیم هشدار قیمت</b>\n\n✅ ارز: <b>{sym}</b>  (اکنون {price})\n✅ جهت: {arrow} <b>{direction}</b>\n\nمرحله ۳ از ۳ — قیمت هدف را بفرستید:\n<i>مثال: 95000</i>\n\nبرای لغو /cancel بزنید.",
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
        "برای لغو /cancel بزنید.",
        'digest_time_confirm': "✅ زمان خلاصه روی <b>{hour}:00</b> تنظیم شد.",
        'digest_morning':      "☀️ <b>صبح بخیر! پرتفوی شما</b>\n",
        'digest_total':        "\n💰 <b>جمع: {usd}</b> · {irr} تومان",
        'btn_portfolio':       "💼 پرتفو",
        'btn_alerts':          "🔔 هشدارها",

        # ── TRON transaction ──────────────────────────────────
        'tx_header':           "ℹ️ جزئیات تراکنش ترون\n\n",
        'tx_status':           "{emoji} وضعیت: {status}\n\n",
        'tx_confirmed':        "تأیید شده",
        'tx_pending':          "در انتظار",
        'tx_block':            "🔗 بلوک: #{block}\n\n",
        'tx_time':             "🕐 زمان: {time}\n\n",
        'tx_from':             "📤 از:\n{addr}\n\n",
        'tx_to':               "📥 به:\n{addr}\n\n",
        'tx_amount':           "💰 مقدار: {amount} TRX\n\n",
        'tx_fee':              "⛽ کارمزد شبکه: {fee} TRX\n\n",
        'tx_hash':             "📝 هش تراکنش:\n{hash}\n",
        'tx_not_found':        "❌ تراکنش پیدا نشد. هش را بررسی کنید.",
        'tx_timeout':          "⏳ درخواست منقضی شد. دوباره امتحان کنید.",
        'tx_error':            "❌ دریافت تراکنش ممکن نشد. هش را بررسی کنید.",

        # ── inline / misc ─────────────────────────────────────
        'inline_tips_title':   "ربات کریپتو — راهنمای سریع",
        'inline_tips_desc':    "btc · 10trx · 100usd to eth · u · ولت · hash…",
        'inline_tips_body':
            "<b>راهنمای حالت Inline:</b>\n\n"
            "• <code>btc</code> — قیمت\n"
            "• <code>10trx</code> یا <code>10 trx</code> — ارزش\n"
            "• <code>u</code> یا <code>10u</code> — تتر (USDT)\n"
            "• <code>100usd to eth</code> — تبدیل\n"
            "• <code>usd</code> — نرخ دلار\n"
            "• <code>price</code> — همه قیمت‌ها\n"
            "• <code>ولت</code> — با موجودی  |  <code>ولت آدرس</code> — فقط آدرس\n"
            "• <code>10+20*3</code> — ماشین حساب\n"
            "• <code>15% of 200</code>  |  <code>100+5%</code>  |  <code>80-20%</code> — درصد\n"
            "• لینک تراکنش TRON یا hash — جزئیات",
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
        'gdpr_cancelled':      "لغو شد — داده‌های شما در امان هستند.",

        # ── Currency / price display ──────────────────────────
        'toman_label':         "تومان",
        'convert_toman_note':  "\n💰 {irr} تومان",
        'convert_toman_approx':"\n💰 ≈ {usd} · {irr} تومان",
        'price_toman_line':    "💰 {irr} تومان\n",
        'inline_usd_toman':    "دلار → تومان",
        'inline_toman_usd':    "تومان → دلار",
        'usd_simple':          "1 USD = {rate} Toman",
        'usd_amount':          "{amount} USD = {rate} Toman",

        # ── Exchange / Chart / Trending / Fragment ─────────────
        'exch_set':            "✅ منبع قیمت پیش‌فرض به <b>{name}</b> تغییر یافت",
        'exch_unknown':        "❌ صرافی ناشناخته. انتخاب کنید: {list}",
        'exch_usage':          "نحوه استفاده: /setexchange binance\nیا @exchange به هر query اضافه کنید: btc @binance",
        'exch_prompt':         "💱 <b>منبع قیمت پیش‌فرض را انتخاب کنید:</b>",
        'chart_usage':         "نحوه استفاده:\n/chart btc\n/chart btc 7d\n/chart btc 90d",
        'chart_fail':          "❌ تولید نمودار ناموفق بود. دوباره تلاش کنید.",
        'compare_usage':       "نحوه استفاده: /compare btc eth\n/compare btc sol 90d",
        'compare_fail':        "❌ مقایسه ناموفق بود. دوباره تلاش کنید.",
        'trending_header':     "🔥 <b>محبوب‌های CoinGecko</b>\n",
        'trending_fail':       "❌ دریافت داده محبوب‌ها ناموفق بود.",
        'gainers_header':      "📈 <b>بیشترین افزایش (24h)</b>\n",
        'losers_header':       "📉 <b>بیشترین کاهش (24h)</b>\n",
        'movers_fail':         "❌ دریافت داده بازار ناموفق بود.",
        'fragment_usage':      "نحوه استفاده: /fragment @username\nبررسی نام کاربری تلگرام در Fragment.com",
        'fragment_looking':    "🔍 در حال بررسی <b>{target}</b> در Fragment...",
        'fragment_fail':       "❌ دریافت داده از Fragment ناموفق بود.",
        'fragment_owned':      "مالکیت / قابل فروش نیست",
        'fragment_for_sale':   "برای فروش",
        'fragment_auction':    "در حراج",
        'fragment_status':     "📌 وضعیت: <b>{status}</b>",
        'fragment_price':      "💰 قیمت: <b>{ton} TON</b>{usd}",
        'fragment_owner':      "👤 مالک: <code>{wallet}</code>",
        'gifts_header':        "🎁 <b>مجموعه هدایای تلگرام</b>\n",
        'gifts_fail':          "❌ دریافت مجموعه هدایا ناموفق بود.",
        'gifts_usage':         "نحوه استفاده: /gift collection_slug\nمثال: /gift astralshard",
        'gift_not_found':      "❌ مجموعه <b>{slug}</b> یافت نشد. از /gifts استفاده کنید.",
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
    'cardano': '🪙 Cardano (ADA)',
    'ripple': '🪙 Ripple (XRP)',
    'solana': '🪙 Solana (SOL)',
    'polkadot': '🪙 Polkadot (DOT)',
    'dogecoin': '🪙 Dogecoin (DOGE)',
    'shiba-inu': '🪙 Shiba Inu (SHIB)',
    'tron': '🪙 Tron (TRX)',
    'the-open-network': '🪙 TON (TON)',
    'telegram-stars': '⭐ Telegram Stars (STARS)'
}

CRYPTO_ALIASES = {
    'btc': 'bitcoin', 'بیتکوین': 'bitcoin', 'بیت کوین': 'bitcoin', 'بیت‌کوین': 'bitcoin', 'bitcoin': 'bitcoin',
    'بیت': 'bitcoin',
    'eth': 'ethereum', 'اتریوم': 'ethereum', 'اتر': 'ethereum', 'ethereum': 'ethereum',
    'usdt': 'tether', 'تتر': 'tether', 'tether': 'tether',
    'bnb': 'binancecoin', 'بایننس': 'binancecoin', 'binance': 'binancecoin',
    'بینانس': 'binancecoin', 'بی ان بی': 'binancecoin',
    'ada': 'cardano', 'کاردانو': 'cardano', 'cardano': 'cardano',
    'کاردان': 'cardano', 'آدا': 'cardano',
    'xrp': 'ripple', 'ریپل': 'ripple', 'ripple': 'ripple',
    'ریپ': 'ripple', 'ایکس آر پی': 'ripple',
    'sol': 'solana', 'سولانا': 'solana', 'solana': 'solana',
    'سول': 'solana',
    'dot': 'polkadot', 'پولکادات': 'polkadot', 'polkadot': 'polkadot',
    'پولکا': 'polkadot', 'دات': 'polkadot',
    'doge': 'dogecoin', 'دوج': 'dogecoin', 'دوج کوین': 'dogecoin', 'dogecoin': 'dogecoin',
    'دوگ': 'dogecoin',
    'shib': 'shiba-inu', 'شیبا': 'shiba-inu', 'shiba': 'shiba-inu',
    'شیبا اینو': 'shiba-inu',
    'trx': 'tron', 'ترون': 'tron', 'tron': 'tron',
    'تی آر ایکس': 'tron',
    'ton': 'the-open-network', 'تون': 'the-open-network', 'toncoin': 'the-open-network',
    'تانکوین': 'the-open-network',
    'stars': 'telegram-stars', 'star': 'telegram-stars', 'استار': 'telegram-stars', 
    'استارز': 'telegram-stars', 'ستاره': 'telegram-stars', 'telegram': 'telegram-stars',
}

FIAT_ALIASES = {
    'usd': 'usd', 'دلار': 'usd', 'dollar': 'usd', 'dollars': 'usd', '$': 'usd',
    'toman': 'toman', 'تومان': 'toman', 'تومن': 'toman', 'irr': 'toman', 'ریال': 'toman'
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


def get_ton_wallet_balance(address, user_id: int = 0):
    """
    Fetch TON wallet balance using TON API.
    """
    try:
        # Using TON Center API (public)
        url = f"https://toncenter.com/api/v2/getAddressInformation?address={address}"
        response = requests.get(url, timeout=10)
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
                return f"👛 TON Wallet Balance\n\n🪙 {bal_str} TON"
            
            # Enhanced display with crypto + USD + Toman
            balance_display = format_wallet_balance(
                crypto_amount=balance_ton,
                crypto_symbol='TON',
                usd_rate=Decimal(str(ton_price)),
                toman_rate=Decimal(str(usd_to_irr)),
                user_lang=user_lang
            )
            
            return f"👛 TON Wallet Balance\n\n{balance_display}"
        
        return "❌ Could not fetch wallet balance"
        
    except requests.Timeout:
        logger.error(f"Timeout fetching TON wallet: {address}")
        return "⏳ Request timed out. Try again."
    except Exception as e:
        logger.error(f"Error fetching TON wallet: {e}")
        return "❌ Error fetching wallet. Check address."


def get_ton_transaction_details(hash_value, user_id: int = 0):
    """
    Fetch TON transaction details.
    Primary: TonViewer API
    Secondary: TonScan fallback
    """
    # Try TonViewer API first (most accurate)
    try:
        url = f"https://tonapi.io/v2/blockchain/transactions/{hash_value}"
        headers = {'accept': 'application/json'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Parse transaction
        lt = data.get('lt', 'N/A')
        utime = data.get('utime', 0)
        time_str = datetime.fromtimestamp(utime).strftime('%Y-%m-%d %H:%M:%S') if utime else 'N/A'
        
        # Get account info
        account = data.get('account', {})
        account_address = account.get('address', 'N/A')
        
        # Get in_msg (incoming message)
        in_msg = data.get('in_msg', {})
        source = in_msg.get('source', {})
        source_address = source.get('address', 'N/A') if source else 'N/A'
        
        destination = in_msg.get('destination', {})
        dest_address = destination.get('address', 'N/A') if destination else 'N/A'
        
        # Get value
        value_nano = int(in_msg.get('value', 0))
        value_ton = value_nano / 1_000_000_000
        
        # If in_msg doesn't have clear source/dest, try out_msgs
        if source_address == 'N/A' or dest_address == 'N/A':
            out_msgs = data.get('out_msgs', [])
            if out_msgs:
                first_out = out_msgs[0]
                if source_address == 'N/A':
                    src = first_out.get('source', {})
                    source_address = src.get('address', account_address) if src else account_address
                if dest_address == 'N/A':
                    dst = first_out.get('destination', {})
                    dest_address = dst.get('address', 'N/A') if dst else 'N/A'
                if value_ton == 0:
                    value_nano = int(first_out.get('value', 0))
                    value_ton = value_nano / 1_000_000_000
        
        # Build result
        result = (
            f"ℹ️ <b>TON Transaction</b>\n\n"
            f"🕐 Time: {time_str}\n\n"
        )
        
        if source_address != 'N/A':
            result += f"📤 From:\n<code>{source_address}</code>\n\n"
        
        if dest_address != 'N/A':
            result += f"📥 To:\n<code>{dest_address}</code>\n\n"
        
        if value_ton > 0:
            result += f"💰 Amount: {value_ton:.4f} TON\n\n"
        
        result += f"📝 Hash:\n<code>{hash_value}</code>\n\n"
        result += (
            f"🔗 <a href='https://tonviewer.com/transaction/{hash_value}'>TonViewer</a> · "
            f"<a href='https://tonscan.org/tx/{hash_value}'>TonScan</a>"
        )
        
        return result
        
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            # Try TonScan as fallback
            logger.info(f"TonViewer 404, trying TonScan for {hash_value}")
            return _get_ton_tx_fallback(hash_value)
        logger.error(f"TonViewer HTTP error: {e}")
        return _get_ton_tx_fallback(hash_value)
    except requests.Timeout:
        logger.error(f"TonViewer timeout for {hash_value}")
        return _get_ton_tx_fallback(hash_value)
    except Exception as e:
        logger.error(f"TonViewer error: {e}")
        return _get_ton_tx_fallback(hash_value)


def _get_ton_tx_fallback(hash_value):
    """
    Fallback method using TonScan API when TonViewer fails.
    """
    try:
        url = f"https://toncenter.com/api/v3/transactionsByMessage?msg_hash={hash_value}"
        headers = {'accept': 'application/json'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        transactions = data.get('transactions', [])
        if not transactions:
            return "❌ Transaction not found"
        
        tx = transactions[0]
        
        # Parse transaction
        utime = tx.get('utime', 0)
        time_str = datetime.fromtimestamp(utime).strftime('%Y-%m-%d %H:%M:%S') if utime else 'N/A'
        
        # Get addresses
        account_addr = tx.get('account', 'N/A')
        
        # Get in message
        in_msg = tx.get('in_msg', {})
        source = in_msg.get('source', 'N/A')
        destination = in_msg.get('destination', 'N/A')
        value_nano = int(in_msg.get('value', 0))
        value_ton = value_nano / 1_000_000_000
        
        # Build result
        result = (
            f"ℹ️ <b>TON Transaction</b>\n"
            f"<i>(via TonScan fallback)</i>\n\n"
            f"🕐 Time: {time_str}\n\n"
        )
        
        if source and source != 'N/A':
            result += f"📤 From:\n<code>{source}</code>\n\n"
        
        if destination and destination != 'N/A':
            result += f"📥 To:\n<code>{destination}</code>\n\n"
        
        if value_ton > 0:
            result += f"💰 Amount: {value_ton:.4f} TON\n\n"
        
        result += f"📝 Hash:\n<code>{hash_value}</code>\n\n"
        result += (
            f"🔗 <a href='https://tonviewer.com/transaction/{hash_value}'>TonViewer</a> · "
            f"<a href='https://tonscan.org/tx/{hash_value}'>TonScan</a>"
        )
        
        return result
        
    except Exception as e:
        logger.error(f"TonScan fallback error: {e}")
        return (
            f"❌ Could not fetch transaction details\n\n"
            f"📝 Hash: <code>{hash_value}</code>\n\n"
            f"🔗 <a href='https://tonviewer.com/transaction/{hash_value}'>View on TonViewer</a>"
        )

# ─────────────────────────────────────────────
# API helpers
# ─────────────────────────────────────────────
@rate_limited_api_call
def get_crypto_price(crypto_id):
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
    
    # Try multiple sources in order
    # 1) CoinGecko
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={crypto_id}&vs_currencies=usd"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if crypto_id in data:
                price = data[crypto_id]['usd']
                cache_set(crypto_id, price)
                return price
    except Exception as e:
        logger.error(f"CG single {crypto_id}: {e}")
    
    # 2) CoinCap
    try:
        cap_id = _COINCAP_ID_MAP.get(crypto_id, crypto_id)
        resp = requests.get(f"https://api.coincap.io/v2/assets/{cap_id}", timeout=10)
        if resp.status_code == 200:
            d = resp.json().get('data', {})
            price = float(d.get('priceUsd', 0))
            if price:
                cache_set(crypto_id, price)
                return price
    except Exception as e:
        logger.error(f"CoinCap single {crypto_id}: {e}")
    
    # 3) Binance (via exchange ticker)
    try:
        sym = EXCHANGE_SYMBOL_MAP.get(crypto_id)
        if sym:
            resp = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={'symbol': f"{sym}USDT"},
                timeout=10
            )
            if resp.status_code == 200:
                price = float(resp.json().get('price', 0))
                if price:
                    cache_set(crypto_id, price)
                    return price
    except Exception as e:
        logger.error(f"Binance single {crypto_id}: {e}")
    
    # 4) CryptoCompare (existing fallback)
    try:
        symbol_map = {
            'bitcoin': 'BTC', 'ethereum': 'ETH', 'tether': 'USDT', 'binancecoin': 'BNB',
            'cardano': 'ADA', 'ripple': 'XRP', 'solana': 'SOL', 'polkadot': 'DOT',
            'dogecoin': 'DOGE', 'shiba-inu': 'SHIB', 'tron': 'TRX', 'the-open-network': 'TON'
        }
        symbol = symbol_map.get(crypto_id)
        if symbol:
            resp = requests.get(f"https://min-api.cryptocompare.com/data/price?fsym={symbol}&tsyms=USD", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if 'USD' in data:
                    price = float(data['USD'])
                    cache_set(crypto_id, price)
                    return price
    except Exception as e:
        logger.error(f"CryptoCompare {crypto_id}: {e}")
    
    return None


EXCHANGE_SYMBOL_MAP = {
    'bitcoin': 'BTC', 'ethereum': 'ETH', 'tether': 'USDT',
    'binancecoin': 'BNB', 'cardano': 'ADA', 'ripple': 'XRP',
    'solana': 'SOL', 'polkadot': 'DOT', 'dogecoin': 'DOGE',
    'shiba-inu': 'SHIB', 'tron': 'TRX', 'the-open-network': 'TON',
}

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
        resp = requests.get(url, timeout=8)
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
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT exchange FROM exchange_prefs WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()
    return row[0] if row else 'coingecko'


def set_user_exchange(user_id, exchange):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO exchange_prefs (user_id, exchange) VALUES (?,?)", (user_id, exchange))
        conn.commit()
        conn.close()


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
    data = cg.get_coin_market_chart_by_id(id=crypto_id, vs_currency='usd', days=days)
    return data.get('prices', [])


def get_crypto_chart_image(crypto_id, days=30, user_id=0):
    cache_key = f'chart_{crypto_id}_{days}'
    cached = cache_get(cache_key)
    if cached is not None:
        return cached, crypto_id.upper()
    try:
        raw_prices = _fetch_chart_data(crypto_id, days)
        if not raw_prices:
            raise ValueError("No price data returned")

        timestamps = [p[0] for p in raw_prices]
        prices = [p[1] for p in raw_prices]
        dates = [datetime.fromtimestamp(ts / 1000) for ts in timestamps]

        fig, ax = plt.subplots(figsize=(6, 3.8))
        fig.patch.set_facecolor('#0e1117')
        ax.set_facecolor('#0e1117')
        ax.plot(dates, prices, color='#00cc96', linewidth=1.5)
        ax.grid(True, color='gray', linestyle='--', linewidth=0.3, alpha=0.3)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_color('#555')
        ax.spines['bottom'].set_color('#555')
        ax.tick_params(axis='x', colors='#aaa', labelsize=8, rotation=30)
        ax.tick_params(axis='y', colors='#aaa', labelsize=8)
        ax.set_title(f"{crypto_id.upper()} — {days}d", color='#ccc', fontsize=11, pad=8)
        ax.set_ylabel("USD", color='#aaa', fontsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))

        buf = BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight', dpi=80,
                    facecolor=fig.get_facecolor(), edgecolor='none')
        buf.seek(0)
        plt.close(fig)
        result = buf.getvalue()
        cache_set(cache_key, result, ttl=21600)
        return result, crypto_id.upper()
    except Exception as e:
        logger.error(f"Chart generation failed for {crypto_id}: {e}")
        raise


TOP_COINS_FOR_CHART = [
    'bitcoin', 'ethereum', 'tether', 'binancecoin', 'cardano',
    'ripple', 'solana', 'polkadot', 'dogecoin', 'shiba-inu',
    'tron', 'the-open-network',
]


def _prewarm_charts():
    """Pre-generate charts for top coins so the first request is instant."""
    logger.info("Pre-warming chart cache for top coins...")
    for cid in TOP_COINS_FOR_CHART:
        try:
            cache_key = f'chart_{cid}_30'
            if cache_get(cache_key) is None:
                get_crypto_chart_image(cid)
                logger.debug(f"Pre-warmed chart for {cid}")
        except Exception as e:
            logger.debug(f"Pre-warm skipped for {cid}: {e}")
    logger.info("Chart pre-warming complete")


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

    fig, ax = plt.subplots(figsize=(7, 7))
    fig.patch.set_facecolor('#0e1117')
    ax.set_facecolor('#0e1117')

    wedge_colours = [colours_pool[i % len(colours_pool)] for i in range(len(labels))]
    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, colors=wedge_colours,
        autopct='%1.1f%%', startangle=140,
        textprops={'color': 'white', 'fontsize': 11},
        wedgeprops={'linewidth': 1.5, 'edgecolor': '#0e1117'}
    )
    for at in autotexts:
        at.set_fontsize(9)
        at.set_color('white')

    total = sum(sizes)
    ax.set_title(
        f"Portfolio Breakdown  (Total: ${total:,.2f})",
        color='white', fontsize=13, pad=20
    )
    buf = BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=120,
                facecolor=fig.get_facecolor(), edgecolor='none')
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()


@rate_limited_api_call
def get_usd_to_irr():
    cached = cache_get('usd_to_irr')
    if cached is not None:
        return cached
    try:
        response = requests.get(
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
                cache_set('usd_to_irr', price)
                logger.info(f"Fetched USD to IRR rate: {price}")
                return price
            best_buy = usdt_stats.get('bestBuy')
            best_sell = usdt_stats.get('bestSell')
            if best_buy and best_sell:
                avg_price = (float(best_buy) + float(best_sell)) / 2
                price = int(avg_price / 10)
                cache_set('usd_to_irr', price)
                return price
    except Exception as e:
        logger.error(f"Error fetching USD/IRR: {e}")

    logger.warning("All USD/IRR sources failed — rate unavailable.")
    if OWNER_USER_ID:
        try:
            bot.send_message(
                OWNER_USER_ID,
                f"⚠️ <b>USD/IRR API failed</b>\n\nBoth Nobitex sources failed. "
                f"Rate is unavailable.\n\n"
                f"<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>",
                parse_mode='HTML'
            )
        except Exception:
            pass
    return None


@rate_limited_api_call
def get_gold_prices():
    """Fetch gold prices from CoinGecko (XAU in USD)."""
    cached = cache_get('gold_xau')
    if cached is not None:
        return {'xau': cached}
    
    try:
        # CoinGecko: Gold (XAU) in USD
        url = "https://api.coingecko.com/api/v3/simple/price?ids=pax-gold&vs_currencies=usd"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if 'pax-gold' in data and 'usd' in data['pax-gold']:
            xau_price = data['pax-gold']['usd']
            cache_set('gold_xau', xau_price)
            logger.info(f"Fetched gold price: ${xau_price}")
            return {'xau': xau_price}
    except Exception as e:
        logger.error(f"CoinGecko gold error: {e}")
    
    # Fallback to Binance (PAXG/USDT - gold-backed token)
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if 'price' in data:
            xau_price = float(data['price'])
            cache_set('gold_xau', xau_price)
            logger.info(f"Fetched gold price from Binance: ${xau_price}")
            return {'xau': xau_price}
    except Exception as e:
        logger.error(f"Binance gold error: {e}")
    
    return {}  # Return empty if all fail


@rate_limited_api_call
def get_try_to_irr():
    """Turkish Lira → Toman rate."""
    cached = cache_get('try_irr')
    if cached:
        return cached
    try:
        # Method: TRY → USD → IRR
        # Get TRY/USD rate
        r = requests.get('https://api.exchangerate-api.com/v4/latest/TRY', timeout=8)
        if r.status_code == 200:
            try_data = r.json()
            try_to_usd = try_data.get('rates', {}).get('USD')
            if try_to_usd:
                usd_to_irr = get_usd_to_irr()
                if usd_to_irr is None:
                    return None
                try_to_irr_rate = int(try_to_usd * usd_to_irr)
                cache_set('try_irr', try_to_irr_rate)
                logger.info(f"TRY rate: {try_to_irr_rate} Toman")
                return try_to_irr_rate
    except Exception as e:
        logger.error(f"TRY fetch failed: {e}")
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
        return "—"
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
    if lang == 'fa':
        en_to_fa = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')
        return value.translate(en_to_fa)
    return value


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

        # Evaluate safely
        try:
            if SAFE_EVAL_AVAILABLE:
                # Use simpleeval for maximum safety
                from simpleeval import simple_eval
                result = simple_eval(sanitized)
            else:
                if not sanitized or sanitized == '.':
                    return T(user_id, 'invalid_expression')
                try:
                    result = ast.literal_eval(sanitized)
                except (ValueError, SyntaxError, MemoryError):
                    return T(user_id, 'invalid_expression')
        except (SyntaxError, NameError, TypeError, ValueError):
            return T(user_id, 'invalid_expression')

        # Format result
        if isinstance(result, float) and result == int(result):
            result = int(result)
        expr_display = _normalize_persian(original_expr)
        return T(user_id, 'math_result', expr=expr_display, result=f"{result:,}")
    except ZeroDivisionError:
        return T(user_id, 'division_by_zero')
    except Exception as e:
        logger.error(f"Math evaluation error: {e}")
        return T(user_id, 'invalid_expression')


# ─────────────────────────────────────────────
# TRON helpers
# ─────────────────────────────────────────────
def get_tron_wallet_trx(address, user_id: int = 0):
    try:
        url = f"https://apilist.tronscan.org/api/account?address={address}"
        response = requests.get(url, timeout=10)
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
                return f"👛 TRON Wallet Balance\n\n🪙 {bal_str} TRX"
            
            balance_display = format_wallet_balance(
                crypto_amount=balance_trx,
                crypto_symbol='TRX',
                usd_rate=Decimal(str(trx_price)),
                toman_rate=Decimal(str(usd_to_irr)),
                user_lang=user_lang
            )
            
            return f"👛 TRON Wallet Balance\n\n{balance_display}"
        return T(user_id, 'no_balance')
    except requests.Timeout:
        logger.error(f"Timeout fetching TRX wallet: {address}")
        return T(user_id, 'tron_timeout')
    except Exception as e:
        logger.error(f"Error fetching TRX wallet: {e}")
        return T(user_id, 'tron_error')


def get_tron_transaction_details(hash_value, user_id: int = 0):
    try:
        url = f"https://apilist.tronscan.org/api/transaction-info?hash={hash_value}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data or 'hash' not in data:
            return T(user_id, 'tx_not_found')
        timestamp = data.get('timestamp', 0)
        time_str = datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S') if timestamp else 'N/A'
        confirmed = data.get('confirmed', False)
        status_emoji = '✅' if confirmed else '⏳'
        status_text = T(user_id, 'tx_confirmed') if confirmed else T(user_id, 'tx_pending')
        result = (
            T(user_id, 'tx_header') +
            T(user_id, 'tx_status', emoji=status_emoji, status=status_text) +
            T(user_id, 'tx_block', block=f"{data.get('block', 'N/A'):,}") +
            T(user_id, 'tx_time', time=time_str)
        )
        if 'contractData' in data:
            contract = data['contractData']
            owner = contract.get('owner_address', 'N/A')
            to = contract.get('to_address', 'N/A')
            amount = contract.get('amount', 0)
            # Make addresses copyable with code tags
            result += T(user_id, 'tx_from', addr=f"<code>{owner}</code>")
            result += T(user_id, 'tx_to', addr=f"<code>{to}</code>")
            if amount:
                result += T(user_id, 'tx_amount', amount=f"{float(amount) / 1_000_000:,.6f}")
        if 'cost' in data:
            fee = float(data['cost'].get('net_fee', 0)) / 1_000_000
            energy_fee = float(data['cost'].get('energy_fee', 0)) / 1_000_000
            total_fee = fee + energy_fee
            if total_fee > 0:
                result += T(user_id, 'tx_fee', fee=f"{total_fee:,.6f}")
        # Make hash copyable with code tag and add Tronscan link
        result += T(user_id, 'tx_hash', hash=f"<code>{hash_value}</code>")
        result += f"\n\n🔗 <a href='https://tronscan.org/#/transaction/{hash_value}'>View on Tronscan</a>"
        return result
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
    # Special case: 'u' → 'usdt' only when check_u_alias=True (number present)
    if check_u_alias and text_lower == 'u':
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
def start(message):
    user_id = message.from_user.id
    name = message.from_user.first_name or "there"
    # Force-join check
    if REQUIRED_CHANNEL and _is_joined_channel(user_id) is False:
        _send_join_required(message.chat.id)
        return
    # On first /start, show language picker if no language set yet
    if message.text and message.text.strip().lower() == '/start':
        import sqlite3 as _sq
        with db_lock:
            _c = _sq.connect(DB_FILE)
            cur = _c.cursor()
            cur.execute("SELECT lang FROM user_languages WHERE user_id=?", (user_id,))
            _row = cur.fetchone()
            _c.close()
        if _row is None:
            _send_language_picker(message.chat.id)
            return
    bot.send_message(
        message.chat.id,
        T(user_id, 'start_welcome', name=name),
        parse_mode='HTML'
    )
    logger.info(f"User {user_id} started the bot")


@bot.message_handler(commands=['cancel'])
@rate_limit_check
def cancel(message):
    user_id = message.from_user.id
    if user_id in user_state:
        del_user_state(user_id)
        bot.reply_to(message, T(user_id, 'cancelled'), parse_mode='HTML')
    else:
        bot.reply_to(message, T(user_id, 'nothing_to_cancel'))


def _send_language_picker(chat_id):
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
def language_cmd(message):
    _send_language_picker(message.chat.id)


@bot.message_handler(commands=['privacy'])
@rate_limit_check
def privacy_cmd(message):
    user_id = message.from_user.id
    bot.reply_to(message, T(user_id, 'privacy_text'), parse_mode='HTML')


@bot.message_handler(commands=['deleteaccount'])
@rate_limit_check
def delete_account_cmd(message):
    user_id = message.from_user.id
    kb = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(T(user_id, 'btn_delete_yes'), callback_data="gdpr_delete_confirm"),
        types.InlineKeyboardButton(T(user_id, 'btn_cancel'),     callback_data="gdpr_delete_cancel"),
    ]])
    bot.reply_to(message, T(user_id, 'delete_confirm_prompt'), parse_mode='HTML', reply_markup=kb)


@bot.message_handler(commands=['suggest', 'ticket'])
@rate_limit_check
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
def donate_cmd(message):
    user_id = message.from_user.id
    parts = []
    parts.append(T(user_id, 'donate_info'))
    if DONATION_LINK:
        parts.append(f"🌐 <b>Link:</b>\n{DONATION_LINK}")
    if DONATION_WALLETS:
        chains_list = "\n".join(
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
    if not is_valid_tron_address(address):
        bot.send_message(
            chat_id,
            T(user_id, 'wallet_invalid', address=address),
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
            T(user_id, 'wallet_added', address=address, count=len(existing)+1, max=MAX_WALLETS_PER_USER),
            parse_mode='HTML',
            reply_markup=kb
        )
        logger.info(f"User {user_id} added wallet {address[:6]}…{address[-4:]}")
    else:
        bot.send_message(chat_id, T(user_id, 'wallet_already_saved'))


# ─────────────────────────────────────────────
# Inline keyboard builders
# ─────────────────────────────────────────────

def build_wallets_keyboard(wallets: list[str], user_id: int = 0) -> types.InlineKeyboardMarkup:
    """One row per wallet: [🔗 Txxx…xxx]  [🗑 Remove]
    Plus an ➕ Add Wallet button at the bottom."""
    keyboard = []
    for i, addr in enumerate(wallets):
        short = f"{addr[:6]}…{addr[-4:]}"
        keyboard.append([
            types.InlineKeyboardButton(f"🔗 {short}", callback_data=f"wnoop_{i}"),
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
    lines = [T(user_id, 'wallets_header')]
    for i, addr in enumerate(wallets, 1):
        lines.append(f"{i}. <code>{addr}</code>")
    return "\n".join(lines)


def holdings_message_text(holdings: dict, usd_to_irr, buy_prices: dict = None, user_id: int = 0) -> str:
    if not holdings:
        return T(user_id, 'no_holdings')
    buy_prices = buy_prices or {}
    lines = [T(user_id, 'portfolio_header')]
    total_usd = 0.0
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
                lines.append(line)
            else:
                lines.append(f"🪙 <b>{symbol}</b>  {amount:,.6g} · {T(user_id, 'price_unavail_short')}")
        else:
            lines.append(f"🪙 <b>{symbol}</b>  {amount:,.6g}")
    if usd_to_irr:
        total_irr = total_usd * usd_to_irr
        lines.append(T(user_id, 'portfolio_total', usd=fmt_price(total_usd), irr=f"{total_irr:,.0f}"))
    else:
        lines.append(T(user_id, 'portfolio_total', usd=fmt_price(total_usd), irr="N/A"))
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Callback query handler (button taps)
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    data = call.data
    message_id = call.message.message_id

    # ── Force-join verification ────────────────────────────────────────
    if data == "check_join":
        if not REQUIRED_CHANNEL:
            bot.answer_callback_query(call.id, "No channel configured.")
            return
        joined = _is_joined_channel(user_id)
        if joined is True:
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "✅ Welcome! You're verified.")
            # Re-send welcome
            name = call.from_user.first_name or "there"
            import sqlite3 as _sq
            with db_lock:
                _c = _sq.connect(DB_FILE)
                cur = _c.cursor()
                cur.execute("SELECT lang FROM user_languages WHERE user_id=?", (user_id,))
                _row = cur.fetchone()
                _c.close()
            if _row is None:
                _send_language_picker(call.message.chat.id)
            else:
                bot.send_message(
                    call.message.chat.id,
                    T(user_id, 'start_welcome', name=name),
                    parse_mode='HTML'
                )
        else:
            bot.answer_callback_query(call.id, "❌ Not yet joined. Please join the channel first.", show_alert=True)
        return

    # ⭐ ADMIN ACTIONS - Must be BEFORE security check
    if data == "admin_broadcast":
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only", show_alert=True)
            return
        
        set_user_state(user_id, 'admin_broadcast')
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                "📢 <b>Broadcast Message</b>\n\n"
                "Send the message you want to broadcast to all users.\n\n"
                "<i>This will be sent to ALL users who have used the bot.</i>\n\n"
                "To cancel, send /cancel",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
        except:
            pass
        return
    
    if data == "admin_clear_cache":
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only", show_alert=True)
            return
        
        with _cache_lock:
            _cache.clear()
        bot.answer_callback_query(call.id, "✅ Cache cleared!", show_alert=True)
        logger.info(f"Cache cleared by owner {user_id}")
        return
    
    if data == "admin_stats":
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only", show_alert=True)
            return
        
        # Get detailed stats
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
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
            
            conn.close()
        
        msg = "📊 <b>Detailed Statistics</b>\n\n"
        msg += "<b>Users by Language:</b>\n"
        for lang, count in lang_stats:
            lang_name = "English" if lang == 'en' else "Persian"
            msg += f"  {lang_name}: {count}\n"
        
        msg += "\n<b>Top Alert Coins:</b>\n"
        for crypto_id, count in top_alerts:
            name = CRYPTO_LIST.get(crypto_id, crypto_id)
            if '(' in name:
                sym = _sym(crypto_id)
            else:
                sym = crypto_id.upper()
            msg += f"  {sym}: {count} alerts\n"
        
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                add_timestamp(msg),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
        except:
            pass
        return
    
    # ⭐ SECURITY: Check panel ownership (except for language selection)
    if not data.startswith("set_lang_"):
        cleanup_expired_panels()
        
        # Check if panel is registered and belongs to someone else
        if message_id in panel_owners:
            panel = panel_owners[message_id]
            if panel['user_id'] != user_id:
                bot.answer_callback_query(
                    call.id,
                    "⚠️ This panel belongs to another user.\n"
                    "این پنل متعلق به کاربر دیگری است.",
                    show_alert=True
                )
                return

    # ── Language selection ────────────────────────────────────────────
    if data in ("set_lang_en", "set_lang_fa"):
        lang = data.split("_")[2]  # 'en' or 'fa'
        db_set_lang(user_id, lang)
        bot.answer_callback_query(call.id)
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
            T(user_id, 'start_welcome', name=name),
            parse_mode='HTML'
        )
        logger.info(f"User {user_id} set language to {lang}")
        return

    # ── Convert wizard ────────────────────────────────────────────────
    if data.startswith("cvt1_"):
        from_cid = data[5:]
        from_sym = _sym(from_cid)
        coins = list(CRYPTO_LIST.keys()) + ['usd', 'toman']
        rows = []
        row = []
        for cid in coins:
            if cid == from_cid:
                continue
            sym = _sym(cid)
            row.append(types.InlineKeyboardButton(sym, callback_data=f"cvt2_{from_cid}_{cid}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'convert_step2', sym=from_sym),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup(rows)
            )
        except Exception:
            pass
        return

    if data.startswith("cvt2_"):
        _, from_cid, to_cid = data.split("_", 2)
        from_sym = _sym(from_cid)
        to_sym   = _sym(to_cid)
        set_user_state(user_id, f"convert_{from_cid}_{to_cid}")
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'convert_step3', from_sym=from_sym, to_sym=to_sym),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, "btn_cvt_cancel"), callback_data="cvt_cancel")
                ]])
            )
        except Exception:
            pass
        return

    if data == "cvt_cancel":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        if user_id in user_state:
            del_user_state(user_id)
        return

    # ── Compare coin picker (step 1 & 2) ─────────────────────────────
    if data.startswith("cmp1_"):
        cid1 = data[5:]
        filtered = [cid for cid in CRYPTO_LIST if cid != cid1]
        rows = []
        row = []
        for cid in filtered:
            sym = _sym(cid)
            row.append(types.InlineKeyboardButton(sym, callback_data=f"cmp2_{cid1}_{cid}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        sym1 = _sym(cid1)
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'compare_pick2', sym=sym1),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup(rows)
            )
        except Exception:
            pass
        return

    if data.startswith("cmp2_"):
        _, cid1, cid2 = data.split("_", 2)
        if cid1 not in CRYPTO_LIST or cid2 not in CRYPTO_LIST:
            bot.answer_callback_query(call.id, T(user_id, 'unknown_coin_short'))
            return
        bot.answer_callback_query(call.id, T(user_id, 'fetching'))
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
            bot.answer_callback_query(call.id, msg, show_alert=True)
            return
        bot.answer_callback_query(call.id, T(user_id, "refreshing"))
        _do_compare(call.message, cid1, cid2, user_id, edit_msg_id=call.message.message_id)
        return

    # ── Alert wizard (step 1: coin → step 2: direction → step 3: price) ──
    if data == "alrt_cancel":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if data == "alrt_new":
        bot.answer_callback_query(call.id)
        coins = [c for c in CRYPTO_LIST.keys() if c != 'telegram-stars']
        rows = []
        for i in range(0, len(coins), 3):
            row = []
            for cid in coins[i:i+3]:
                sym = _sym(cid)
                row.append(types.InlineKeyboardButton(sym, callback_data=f"alrt1_{cid}"))
            rows.append(row)
        rows.append([types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="alrt_cancel")])
        try:
            bot.send_message(
                call.message.chat.id,
                T(user_id, 'alert_step1'),
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup(rows)
            )
        except Exception:
            pass
        return

    if data.startswith("alrt1_"):
        cid = data[6:]
        if cid not in CRYPTO_LIST:
            bot.answer_callback_query(call.id, T(user_id, 'unknown_coin_short'))
            return
        sym = _sym(cid)
        price = get_crypto_price(cid)
        price_str = fmt_price(price) if price else "—"
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_above'), callback_data=f"alrt2_{cid}_above"),
            types.InlineKeyboardButton(T(user_id, 'btn_below'), callback_data=f"alrt2_{cid}_below"),
        ],[
            types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="alrt_cancel"),
        ]])
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'alert_step2', sym=sym, price=price_str),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=kb
            )
        except Exception:
            pass
        return

    if data.startswith("alrt2_"):
        parts = data.split("_")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, T(user_id, 'invalid_data'))
            return
        cid, direction = parts[1], parts[2]
        sym = _sym(cid)
        price = get_crypto_price(cid)
        price_str = fmt_price(price) if price else "—"
        arrow = "📈" if direction == "above" else "📉"
        set_user_state(user_id, f"alert_price_{cid}_{direction}")
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'alert_step3', sym=sym, price=price_str, arrow=arrow, direction=direction),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="alrt_cancel")
                ]])
            )
        except Exception:
            pass
        return

    if data == "show_alerts":
        bot.answer_callback_query(call.id)
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
        lines = [T(user_id, 'alerts_header', count=len(alerts), max=MAX_ALERTS_PER_USER)]
        for a in alerts:
            arrow = '📈' if a['direction'] == 'above' else '📉'
            cur   = get_crypto_price(a['crypto_id'])
            dword = above_w if a['direction'] == 'above' else below_w
            if cur:
                pct_str = f"{abs((a['target_price']-cur)/cur)*100:.1f}"
                dist = f"  <i>{T(user_id, 'away_pct', pct=pct_str)}</i>"
            else:
                dist = ""
            lines.append(f"{arrow} <b>{a['symbol']}</b> {dword} <b>{fmt_price(a['target_price'])}</b>{dist}")
            keyboard.append([types.InlineKeyboardButton(
                f"🗑  {a['symbol']} {dword} {fmt_price(a['target_price'])}",
                callback_data=f"alertdel_{a['id']}"
            )])
        keyboard.append([
            types.InlineKeyboardButton(T(user_id, 'btn_add_alert'),  callback_data="alrt_new"),
            types.InlineKeyboardButton(T(user_id, 'btn_delete_all'), callback_data="alertdelall"),
        ])
        bot.send_message(
            call.message.chat.id,
            "\n".join(lines),
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
        bot.answer_callback_query(call.id)
        return

    if data.startswith("wrem_"):
        idx = int(data.split("_")[1])
        wallets = db_get_wallets(user_id)
        if idx >= len(wallets):
            bot.answer_callback_query(call.id, T(user_id, 'wallet_not_found'))
            return
        address = wallets[idx]
        db_remove_wallet(user_id, address)
        logger.info(f"User {user_id} removed wallet {address[:6]}…{address[-4:]}")
        wallets = db_get_wallets(user_id)
        try:
            bot.edit_message_text(
                wallets_message_text(wallets),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=build_wallets_keyboard(wallets)
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, T(user_id, 'wallet_removed_toast'))
        return

    if data == "wadd":
        bot.answer_callback_query(call.id)
        set_user_state(user_id, 'add_wallet_inline')
        bot.send_message(call.message.chat.id, T(user_id, 'send_wallet_addr'))
        return

    if data == "hclose" or data == "wclose":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if data == "market_refresh":
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            bot.answer_callback_query(call.id, msg, show_alert=True)
            return
        bot.answer_callback_query(call.id, T(user_id, 'refreshing'))
        market_cmd(call.message, user_id=user_id, edit_msg_id=call.message.message_id)
        return

    # Price list refresh
    if data == "refresh_all_prices":
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            bot.answer_callback_query(call.id, msg, show_alert=True)
            return
        bot.answer_callback_query(call.id, T(user_id, 'refreshing'))
        ids = ','.join(CRYPTO_LIST.keys())
        prices = _fetch_prices_batch(ids)
        usd_to_irr = get_usd_to_irr()
        lines = [T(user_id, 'prices_header')]
        for code, name in CRYPTO_LIST.items():
            p = prices.get(code, {})
            price_usd = p.get('usd')
            change = p.get('usd_24h_change')
            if price_usd is None:
                continue
            cache_set(code, price_usd)
            sym = _sym(code)
            arrow = ('📈' if change >= 0 else '📉') if change is not None else '  '
            chg   = f"{change:+.1f}%" if change is not None else ""
            lines.append(f"{arrow} <b>{sym}</b>  {fmt_price(price_usd)}  <i>{chg}</i>")
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_refresh'), callback_data="refresh_all_prices")
        ]])
        try:
            bot.edit_message_text(
                add_timestamp("\n".join(lines)),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=kb
            )
        except Exception:
            pass
        return

    # Price refresh button on chart messages
    if data.startswith("refresh_"):
        crypto = data[len("refresh_"):]
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            bot.answer_callback_query(call.id, msg, show_alert=True)
            return
        bot.answer_callback_query(call.id, T(user_id, 'refreshing'))
        # Invalidate cache for this coin
        with _cache_lock:
            _cache.pop(crypto, None)
        price_usd = get_crypto_price(crypto)
        usd_to_irr = get_usd_to_irr()
        if not price_usd or usd_to_irr is None:
            bot.answer_callback_query(call.id, T(user_id, 'price_fetch_fail'), show_alert=True)
            return
        price_irr = price_usd * usd_to_irr
        crypto_name = CRYPTO_LIST.get(crypto, crypto.upper())
        refresh_kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_refresh'), callback_data=f"refresh_{crypto}")
        ]])
        new_caption = (f"📊 {crypto_name}\n\n💵 <b>{fmt_price(price_usd)}</b>\n"
                       + T(user_id, 'price_toman_line', irr=f"{price_irr:,.0f}"))
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
        bot.answer_callback_query(call.id)
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM holdings    WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM buy_prices  WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM wallets     WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM alerts      WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM digest_prefs WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM user_languages WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
        del_user_state(user_id)
        with _lang_cache_lock:
            _lang_cache.pop(user_id, None)
        logger.info(f"GDPR delete: all data removed for user {user_id}")
        try:
            bot.edit_message_text(
                T(user_id, 'delete_done'),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
        except Exception:
            pass
        return

    if data == "gdpr_delete_cancel":
        bot.answer_callback_query(call.id, T(user_id, 'gdpr_cancelled'))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if data == "show_holdings":
        bot.answer_callback_query(call.id)
        saved = db_get_holdings(user_id) or {}
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        bot.send_message(
            call.message.chat.id,
            holdings_message_text(saved, usd_to_irr, buy_prices),
            parse_mode='HTML',
            reply_markup=build_holdings_keyboard(saved)
        )
        return

    if data == "show_wallets":
        bot.answer_callback_query(call.id)
        wallets = db_get_wallets(user_id)
        bot.send_message(
            call.message.chat.id,
            wallets_message_text(wallets),
            parse_mode='HTML',
            reply_markup=build_wallets_keyboard(wallets)
        )
        return

    if data.startswith("hnoop_"):
        bot.answer_callback_query(call.id)
        return

    if data.startswith("hrem_"):
        symbol = data[len("hrem_"):]
        bot.answer_callback_query(call.id)
        db_remove_holding(user_id, symbol)
        db_delete_buy_price(user_id, symbol)
        logger.info(f"User {user_id} removed holding {symbol}")
        holdings = db_get_holdings(user_id) or {}
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        try:
            bot.edit_message_text(
                holdings_message_text(holdings, usd_to_irr, buy_prices),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=build_holdings_keyboard(holdings)
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, T(user_id, "holding_removed_toast", sym=symbol))
        return

    if data.startswith("hedit_"):
        symbol = data[len("hedit_"):]
        bot.answer_callback_query(call.id)
        set_user_state(user_id, f'edit_holding_{symbol}')
        bot.send_message(call.message.chat.id, T(user_id, 'edit_amount_prompt', sym=symbol), parse_mode='HTML')
        return

    if data.startswith("hbuy_"):
        symbol = data[len("hbuy_"):]
        bot.answer_callback_query(call.id)
        set_user_state(user_id, f'set_buy_price_{symbol}')
        bot.send_message(call.message.chat.id, T(user_id, 'buy_price_prompt', sym=symbol), parse_mode='HTML')
        return

    if data == "hchart":
        bot.answer_callback_query(call.id, T(user_id, 'generating_chart'))
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
        try:
            img = get_portfolio_chart_image(holdings, prices, user_id)
            bot.send_photo(
                call.message.chat.id,
                photo=BytesIO(img),
                caption=T(user_id, 'chart_caption'),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Portfolio chart failed: {e}")
            bot.send_message(call.message.chat.id, T(user_id, 'chart_fail'))
        return

    if data == "hpick_cancel":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        if user_id in user_state:
            del_user_state(user_id)
        return

    if data.startswith("hpick_"):
        cid = data[6:]
        if cid not in CRYPTO_LIST:
            bot.answer_callback_query(call.id, T(user_id, 'unknown_coin_short'))
            return
        sym   = _sym(cid)
        price = get_crypto_price(cid)
        price_str = T(user_id, 'now_price', price=fmt_price(price)) if price else ""
        set_user_state(user_id, f"hpick_amount_{cid}")
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'coin_amount_prompt', sym=sym, price=price_str),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="hpick_cancel")
                ]])
            )
        except Exception:
            pass
        return

    if data == "hadd":
        bot.answer_callback_query(call.id)
        _show_holding_coin_picker(
            call.message.chat.id,
            T(user_id, 'add_coin_prompt'),
            user_id
        )
        return

    if data == "hclearall":
        bot.answer_callback_query(call.id)
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
        bot.answer_callback_query(call.id)
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
        try:
            bot.edit_message_text(
                T(user_id, 'holdings_cleared'),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
        except Exception:
            pass
        return

    if data == "hclearall_cancel":
        bot.answer_callback_query(call.id, T(user_id, 'cancelled'))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    # ── Chart range selector ──────────────────────────────────
    if data.startswith("chart_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            _, crypto, days_label = parts
            days = CHART_DAYS.get(days_label, 30)
            bot.answer_callback_query(call.id, T(user_id, 'generating_chart'))
            try:
                img_bytes, symbol = get_crypto_chart_image(crypto, days, user_id)
                price = get_crypto_price(crypto)
                kb = types.InlineKeyboardMarkup(row_width=4)
                kb.add(*[types.InlineKeyboardButton(d, callback_data=f"chart_{crypto}_{d}") for d in CHART_DAYS])
                caption = f"📊 <b>{symbol}</b> — {days}d"
                if price:
                    caption += f"\n💵 <b>{fmt_price(price)}</b>"
                bot.edit_message_media(
                    types.InputMediaPhoto(BytesIO(img_bytes), caption=add_timestamp(caption), parse_mode='HTML'),
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    reply_markup=kb
                )
            except Exception as e:
                logger.error(f"Chart callback failed: {e}")
        return

    # ── Exchange selector ─────────────────────────────────────
    if data.startswith("setex_"):
        exch = data.split("_", 1)[1]
        if exch in EXCHANGES or exch == 'coingecko':
            set_user_exchange(user_id, exch)
            name = EXCHANGE_NAMES.get(exch, 'CoinGecko')
            bot.answer_callback_query(call.id, f"✅ Source set to {name}")
            try:
                bot.edit_message_text(
                    f"✅ Default price source set to <b>{name}</b>",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode='HTML'
                )
            except Exception:
                pass
        return

    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['clearwallets'])
@rate_limit_check
def clear_wallets(message):
    user_id = message.from_user.id
    if db_clear_wallets(user_id):
        bot.reply_to(message, T(user_id, 'all_wallets_removed'))
        logger.info(f"User {user_id} cleared wallets")
    else:
        bot.reply_to(message, T(user_id, 'no_wallets_to_remove'))


@bot.message_handler(commands=['wallets'])
@rate_limit_check
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
def show_wallets_with_balance(message):
    user_id = message.from_user.id
    wallets = db_get_wallets(user_id)
    if not wallets:
        bot.reply_to(message, T(user_id, 'no_wallets_yet'))
        return
    bot.send_chat_action(message.chat.id, 'typing')
    reply = T(user_id, 'wallets_balances_hdr')
    for address in wallets:
        balance_msg = get_tron_wallet_trx(address, user_id)
        reply += f"🔗 <code>{address}</code>\n {balance_msg}\n\n"
    bot.reply_to(message, reply, parse_mode='HTML')
    logger.info(f"User {user_id} viewed wallets with balances")


@bot.message_handler(commands=['price'])
@rate_limit_check
def price(message):
    bot.send_chat_action(message.chat.id, 'typing')
    ids = ','.join(CRYPTO_LIST.keys())
    prices = _fetch_prices_batch(ids)
    if not prices:
        bot.reply_to(message, T(message.from_user.id, 'price_unavailable'))
        return

    usd_to_irr = get_usd_to_irr()
    uid_p = message.from_user.id
    lines = [T(uid_p, 'prices_header')]
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
        arrow = ('📈' if change >= 0 else '📉') if change is not None else '  '
        chg   = f"{change:+.1f}% (24h)" if change is not None else ""
        lines.append(f"{arrow} <b>{sym}</b>  {fmt_price(price_usd)}  <i>{chg}</i>")

    kb = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(T(uid_p, 'btn_refresh'), callback_data="refresh_all_prices")
    ]])
    msg = bot.reply_to(message, add_timestamp("\n".join(lines)), parse_mode='HTML', reply_markup=kb)
    register_panel_owner(msg.message_id, message.from_user.id)
    logger.info(f"User {message.from_user.id} requested prices")


@bot.message_handler(commands=['usd'])
@rate_limit_check
def usd_command(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid_u = message.from_user.id
    usd_iran = get_usd_to_irr()
    if usd_iran is None:
        bot.reply_to(message, add_timestamp(T(uid_u, 'rate_unavailable')), parse_mode='HTML')
        return
    rate_str = f"{usd_iran:,.0f}"
    bot.reply_to(
        message,
        add_timestamp(T(uid_u, 'usd_rate', rate=rate_str)),
        parse_mode='HTML'
    )
    logger.info(f"User {message.from_user.id} requested USD → Toman")


@bot.message_handler(commands=['try'])
@rate_limit_check
def try_command(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    try_rate = get_try_to_irr()
    if try_rate is None:
        bot.reply_to(message, add_timestamp(T(uid, 'rate_unavailable')), parse_mode='HTML')
    else:
        bot.reply_to(message, add_timestamp(T(uid, 'try_rate', rate=f"{try_rate:,.0f}")), parse_mode='HTML')
    logger.info(f"User {uid} requested TRY → Toman")


@bot.message_handler(commands=['gold'])
@rate_limit_check
def gold_command(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    prices = get_gold_prices()
    xau_price = prices.get('xau')
    if not xau_price:
        bot.reply_to(message, T(uid, 'gold_fetch_fail'))
        return

    gram_price_usd = xau_price / 31.1035
    user_lang = db_get_lang(uid)
    xau_formatted = format_for_locale(format_fiat(Decimal(str(xau_price))), user_lang)
    gram_usd_formatted = format_for_locale(format_fiat(Decimal(str(gram_price_usd))), user_lang)
    msg = (
        f"🥇 <b>Gold</b>\n\n"
        f"💵 XAU: <b>${xau_formatted}</b>\n"
        f"💵 1g: <b>${gram_usd_formatted}</b>"
    )
    bot.reply_to(message, add_timestamp(msg), parse_mode='HTML')
    logger.info(f"User {uid} requested gold prices")


@bot.message_handler(commands=['stars', 'star'])
@rate_limit_check
def stars_command(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    
    stars_price = get_crypto_price('telegram-stars')
    if not stars_price:
        stars_price = float(os.getenv('STARS_PRICE_USD', '0.015'))
    
    usd_to_irr = get_usd_to_irr()
    ton_price = get_crypto_price('the-open-network')

    user_lang = db_get_lang(uid)
    toman_label = "Toman" if user_lang == 'en' else "تومان"

    msg = f"⭐ <b>Telegram Stars</b>\n\n"
    msg += f"💵 ${stars_price:.3f} USD\n"
    if usd_to_irr:
        msg += f"💰 {format_fiat(Decimal(str(stars_price * usd_to_irr)), decimals=0)} {toman_label}\n"

    if ton_price:
        stars_in_ton = stars_price / ton_price
        msg += f"🪙 {format_crypto(Decimal(str(stars_in_ton)))} TON\n"
    
    bot.reply_to(message, add_timestamp(msg), parse_mode='HTML')
    logger.info(f"User {uid} requested Stars price")


def _show_holding_coin_picker(chat_id, prompt, user_id=0):
    """Show coin picker buttons for add/set holding flows."""
    coins = [c for c in CRYPTO_LIST.keys() if c != 'telegram-stars']
    rows = []
    row = []
    for cid in coins:
        sym = _sym(cid)
        row.append(types.InlineKeyboardButton(sym, callback_data=f"hpick_{cid}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="hpick_cancel")])
    bot.send_message(chat_id, prompt, parse_mode='HTML', reply_markup=types.InlineKeyboardMarkup(rows))


@bot.message_handler(commands=['set'])
@rate_limit_check
def set_holding(message):
    user_id = message.from_user.id
    logger.info(f"User {user_id} initiated set holdings")
    _show_holding_coin_picker(message.chat.id, T(user_id, 'set_holdings_prompt'), user_id)


@bot.message_handler(commands=['holdings'])
@rate_limit_check
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
def convert_cmd(message):
    # Always show the interactive coin picker
    coins = [c for c in CRYPTO_LIST.keys() if c != 'telegram-stars'] + ['usd', 'toman']
    rows = []
    row = []
    for cid in coins:
        sym = _sym(cid)
        row.append(types.InlineKeyboardButton(sym, callback_data=f"cvt1_{cid}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    uid_cv = message.from_user.id
    msg = bot.reply_to(
        message,
        T(uid_cv, 'convert_step1'),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(rows)
    )
    register_panel_owner(msg.message_id, message.from_user.id)


# ═══════════════════════════════════════════════
# Exchange Price Source
# ═══════════════════════════════════════════════
@bot.message_handler(commands=['setexchange'])
@rate_limit_check
def set_exchange_cmd(message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        kb = types.InlineKeyboardMarkup(row_width=2)
        btns = [types.InlineKeyboardButton(info['name'], callback_data=f"setex_{e}") for e, info in EXCHANGES.items()]
        btns.append(types.InlineKeyboardButton("CoinGecko", callback_data="setex_coingecko"))
        kb.add(*btns)
        bot.reply_to(message, "💱 <b>Select your default price source:</b>", parse_mode='HTML', reply_markup=kb)
        return
    exch = args[1].strip().lower()
    if exch not in EXCHANGES and exch != 'coingecko':
        bot.reply_to(message, f"❌ Unknown exchange. Choose: coingecko, {', '.join(EXCHANGES.keys())}")
        return
    set_user_exchange(uid, exch)
    name = EXCHANGE_NAMES.get(exch, 'CoinGecko')
    bot.reply_to(message, f"✅ Default price source set to <b>{name}</b>", parse_mode='HTML')
    logger.info(f"User {uid} set exchange to {exch}")


# ═══════════════════════════════════════════════
# Better Charts
# ═══════════════════════════════════════════════
CHART_DAYS = {'7d': 7, '30d': 30, '90d': 90, '1y': 365}


@bot.message_handler(commands=['chart'])
@rate_limit_check
def chart_cmd(message):
    bot.send_chat_action(message.chat.id, 'upload_photo')
    uid = message.from_user.id
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage:\n/chart btc\n/chart btc 7d\n/chart btc 90d")
        return
    crypto = detect_currency(args[1])
    if not crypto or crypto not in CRYPTO_LIST:
        bot.reply_to(message, T(uid, 'unknown_coin'))
        return
    days = 30
    if len(args) >= 3:
        days = CHART_DAYS.get(args[2].lower(), 30)
    try:
        img_bytes, symbol = get_crypto_chart_image(crypto, days, uid)
        price = get_crypto_price(crypto)
        kb = types.InlineKeyboardMarkup(row_width=4)
        kb.add(*[types.InlineKeyboardButton(d, callback_data=f"chart_{crypto}_{d}") for d in CHART_DAYS])
        caption = f"📊 <b>{symbol}</b> — {days}d"
        if price:
            caption += f"\n💵 <b>{fmt_price(price)}</b>"
        bot.send_photo(
            message.chat.id, photo=BytesIO(img_bytes), caption=add_timestamp(caption),
            parse_mode='HTML', reply_markup=kb
        )
    except Exception as e:
        logger.error(f"Chart failed: {e}")
        bot.reply_to(message, "❌ Chart generation failed. Try again later.")




# ═══════════════════════════════════════════════
# Trending & Market Movers
# ═══════════════════════════════════════════════
TRENDING_CACHE_TTL = 300


def _fetch_trending():
    cached = cache_get('trending_coins')
    if cached:
        return cached
    try:
        resp = requests.get('https://api.coingecko.com/api/v3/search/trending', timeout=10)
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
        resp = requests.get(
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
        return {
            'gainers': coins[:5],
            'losers': coins[-5:][::-1],
        }
    except Exception as e:
        logger.error(f"Gainers/losers fetch failed: {e}")
        return None


@bot.message_handler(commands=['trending'])
@rate_limit_check
def trending_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    data = _fetch_trending()
    if not data:
        bot.reply_to(message, "❌ Could not fetch trending data. Try again later.")
        return
    lines = ["🔥 <b>Trending on CoinGecko</b>\n"]
    for i, coin in enumerate(data, 1):
        rank = coin.get('market_cap_rank')
        rank_str = f"#{rank}" if rank else "—"
        lines.append(f"{i}. <b>{coin['name']}</b> ({coin['symbol']})  ─  Rank {rank_str}")
    bot.reply_to(message, add_timestamp("\n".join(lines)), parse_mode='HTML')


@bot.message_handler(commands=['gainers', 'losers'])
@rate_limit_check
def gainers_losers_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    data = _fetch_gainers_losers()
    if not data:
        bot.reply_to(message, "❌ Could not fetch market data. Try again later.")
        return
    cmd = message.text.strip().lower()
    show_gainers = cmd.startswith('/gainers')

    coins = data['gainers'] if show_gainers else data['losers']
    emoji = '📈' if show_gainers else '📉'
    title = "Top Gainers (24h)" if show_gainers else "Top Losers (24h)"
    lines = [f"{emoji} <b>{title}</b>\n"]
    for c in coins:
        chg = c['price_change_percentage_24h']
        arrow = '📈' if chg >= 0 else '📉'
        price = c.get('current_price')
        price_str = fmt_price(price) if price else '—'
        lines.append(f"{arrow} <b>{c['symbol'].upper()}</b>  {price_str}  <i>{chg:+.2f}%</i>")
    bot.reply_to(message, add_timestamp("\n".join(lines)), parse_mode='HTML')


# ═══════════════════════════════════════════════
# Fragment / Telegram NFT Tracker
# ═══════════════════════════════════════════════
FRAGMENT_CACHE_TTL = 120

FRAGMENT_GIFT_COLLECTIONS = [
    {'slug': 'artisanbrick', 'name': 'Artisan Bricks'},
    {'slug': 'astralshard', 'name': 'Astral Shards'},
    {'slug': 'bdaycandle', 'name': 'B-Day Candles'},
    {'slug': 'berrybox', 'name': 'Berry Boxes'},
    {'slug': 'bigyear', 'name': 'Big Years'},
    {'slug': 'blingbinky', 'name': 'Bling Binkies'},
    {'slug': 'bondedring', 'name': 'Bonded Rings'},
    {'slug': 'bowtie', 'name': 'Bow Ties'},
    {'slug': 'bunnymuffin', 'name': 'Bunny Muffins'},
    {'slug': 'candycane', 'name': 'Candy Canes'},
    {'slug': 'chillflame', 'name': 'Chill Flames'},
    {'slug': 'cloverpin', 'name': 'Clover Pins'},
    {'slug': 'cookieheart', 'name': 'Cookie Hearts'},
    {'slug': 'crystalball', 'name': 'Crystal Balls'},
    {'slug': 'cupidcharm', 'name': 'Cupid Charms'},
    {'slug': 'deskcalendar', 'name': 'Desk Calendars'},
    {'slug': 'diamondring', 'name': 'Diamond Rings'},
    {'slug': 'durovscap', 'name': "Durov's Caps"},
    {'slug': 'easteregg', 'name': 'Easter Eggs'},
    {'slug': 'electricskull', 'name': 'Electric Skulls'},
    {'slug': 'eternalcandle', 'name': 'Eternal Candles'},
    {'slug': 'eternalrose', 'name': 'Eternal Roses'},
    {'slug': 'evileye', 'name': 'Evil Eyes'},
    {'slug': 'faithamulet', 'name': 'Faith Amulets'},
    {'slug': 'flyingbroom', 'name': 'Flying Brooms'},
    {'slug': 'freshsocks', 'name': 'Fresh Socks'},
    {'slug': 'gemsignet', 'name': 'Gem Signets'},
    {'slug': 'genielamp', 'name': 'Genie Lamps'},
    {'slug': 'gingercookie', 'name': 'Ginger Cookies'},
    {'slug': 'hangingstar', 'name': 'Hanging Stars'},
    {'slug': 'happybrownie', 'name': 'Happy Brownies'},
    {'slug': 'heartlocket', 'name': 'Heart Lockets'},
    {'slug': 'heroichelmet', 'name': 'Heroic Helmets'},
    {'slug': 'hexpot', 'name': 'Hex Pots'},
    {'slug': 'holidaydrink', 'name': 'Holiday Drinks'},
    {'slug': 'homemadecake', 'name': 'Homemade Cakes'},
    {'slug': 'hypnolollipop', 'name': 'Hypno Lollipops'},
    {'slug': 'icecream', 'name': 'Ice Creams'},
    {'slug': 'inputkey', 'name': 'Input Keys'},
    {'slug': 'instantramen', 'name': 'Instant Ramens'},
    {'slug': 'iongem', 'name': 'Ion Gems'},
    {'slug': 'ionicdryer', 'name': 'Ionic Dryers'},
    {'slug': 'jackinthebox', 'name': 'Jacks-in-the-Box'},
    {'slug': 'jellybunny', 'name': 'Jelly Bunnies'},
    {'slug': 'jesterhat', 'name': 'Jester Hats'},
    {'slug': 'jinglebells', 'name': 'Jingle Bells'},
    {'slug': 'jollychimp', 'name': 'Jolly Chimps'},
    {'slug': 'joyfulbundle', 'name': 'Joyful Bundles'},
    {'slug': 'khabibspapakha', 'name': "Khabib's Papakhas"},
    {'slug': 'kissedfrog', 'name': 'Kissed Frogs'},
    {'slug': 'lightsword', 'name': 'Light Swords'},
    {'slug': 'lolpop', 'name': 'Lol Pops'},
    {'slug': 'lootbag', 'name': 'Loot Bags'},
    {'slug': 'lovecandle', 'name': 'Love Candles'},
    {'slug': 'lovepotion', 'name': 'Love Potions'},
    {'slug': 'lowrider', 'name': 'Low Riders'},
    {'slug': 'lunarsnake', 'name': 'Lunar Snakes'},
    {'slug': 'lushbouquet', 'name': 'Lush Bouquets'},
    {'slug': 'madpumpkin', 'name': 'Mad Pumpkins'},
    {'slug': 'magicpotion', 'name': 'Magic Potions'},
    {'slug': 'mightyarm', 'name': 'Mighty Arms'},
    {'slug': 'minioscar', 'name': 'Mini Oscars'},
    {'slug': 'moneypot', 'name': 'Money Pots'},
    {'slug': 'moodpack', 'name': 'Mood Packs'},
    {'slug': 'moonpendant', 'name': 'Moon Pendants'},
    {'slug': 'moussecake', 'name': 'Mousse Cakes'},
    {'slug': 'nailbracelet', 'name': 'Nail Bracelets'},
    {'slug': 'nekohelmet', 'name': 'Neko Helmets'},
    {'slug': 'partysparkler', 'name': 'Party Sparklers'},
    {'slug': 'perfumebottle', 'name': 'Perfume Bottles'},
    {'slug': 'petsnake', 'name': 'Pet Snakes'},
    {'slug': 'plushpepe', 'name': 'Plush Pepes'},
    {'slug': 'poolfloat', 'name': 'Pool Floats'},
    {'slug': 'preciouspeach', 'name': 'Precious Peaches'},
    {'slug': 'prettyposy', 'name': 'Pretty Posies'},
    {'slug': 'rarebird', 'name': 'Rare Birds'},
    {'slug': 'recordplayer', 'name': 'Record Players'},
    {'slug': 'restlessjar', 'name': 'Restless Jars'},
    {'slug': 'sakuraflower', 'name': 'Sakura Flowers'},
    {'slug': 'santahat', 'name': 'Santa Hats'},
    {'slug': 'scaredcat', 'name': 'Scared Cats'},
    {'slug': 'sharptongue', 'name': 'Sharp Tongues'},
    {'slug': 'signetring', 'name': 'Signet Rings'},
    {'slug': 'skullflower', 'name': 'Skull Flowers'},
    {'slug': 'skystilettos', 'name': 'Sky Stilettos'},
    {'slug': 'sleighbell', 'name': 'Sleigh Bells'},
    {'slug': 'snakebox', 'name': 'Snake Boxes'},
    {'slug': 'snoopcigar', 'name': 'Snoop Cigars'},
    {'slug': 'snoopdogg', 'name': 'Snoop Doggs'},
    {'slug': 'snowglobe', 'name': 'Snow Globes'},
    {'slug': 'snowmittens', 'name': 'Snow Mittens'},
    {'slug': 'spicedwine', 'name': 'Spiced Wines'},
    {'slug': 'springbasket', 'name': 'Spring Baskets'},
    {'slug': 'spyagaric', 'name': 'Spy Agarics'},
    {'slug': 'starnotepad', 'name': 'Star Notepads'},
    {'slug': 'stellarrocket', 'name': 'Stellar Rockets'},
    {'slug': 'swagbag', 'name': 'Swag Bags'},
    {'slug': 'swisswatch', 'name': 'Swiss Watches'},
    {'slug': 'tamagadget', 'name': 'Tama Gadgets'},
    {'slug': 'timelessbook', 'name': 'Timeless Books'},
    {'slug': 'tophat', 'name': 'Top Hats'},
    {'slug': 'toybear', 'name': 'Toy Bears'},
    {'slug': 'trappedheart', 'name': 'Trapped Hearts'},
    {'slug': 'ufcstrike', 'name': 'UFC Strikes'},
    {'slug': 'valentinebox', 'name': 'Valentine Boxes'},
    {'slug': 'vicecream', 'name': 'Vice Creams'},
    {'slug': 'victorymedal', 'name': 'Victory Medals'},
    {'slug': 'vintagecigar', 'name': 'Vintage Cigars'},
    {'slug': 'voodoodoll', 'name': 'Voodoo Dolls'},
    {'slug': 'westsidesign', 'name': 'Westside Signs'},
    {'slug': 'whipcupcake', 'name': 'Whip Cupcakes'},
    {'slug': 'winterwreath', 'name': 'Winter Wreaths'},
    {'slug': 'witchhat', 'name': 'Witch Hats'},
    {'slug': 'xmasstocking', 'name': 'Xmas Stockings'},
]


def _fragment_username_data(username):
    clean = username.lstrip('@').strip()
    cache_key = f'frag_user_{clean}'
    cached = cache_get(cache_key)
    if cached:
        return cached
    try:
        resp = requests.get(f'https://fragment.com/username/{clean}', timeout=10,
                            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        if resp.status_code != 200:
            return None
        html = resp.text

        status = "Unknown"
        price_ton = None
        owner = None
        auction_end = None
        min_bid = None

        m = re.search(r'tm-section-header-status\s+tm-status-([a-z]+)">([^<]+)', html)
        if m:
            css_class = m.group(1)
            visible_text = m.group(2).strip()
            if css_class == 'unavail':
                status = 'Sold' if visible_text == 'Sold' else 'Unavailable'
            elif css_class == 'taken':
                status = 'Taken'
            elif css_class == 'avail':
                status = visible_text  # "Available" or "On auction"
            else:
                status = visible_text

        m = re.search(r'(\d+[\d,.]*)\s*TON', html)
        if m:
            price_ton = m.group(1).replace(',', '')

        m = re.search(r'(?:Ends? in|Auction ends? in|Time left)[:\s]*([^<]+)', html, re.IGNORECASE)
        if m:
            auction_end = m.group(1).strip()[:60]

        m = re.search(r'Min(?:imum)?\s*(?:bid)?[:\s]*(\d+[\d,.]*)\s*TON', html, re.IGNORECASE)
        if m:
            min_bid = m.group(1).replace(',', '')

        owner = None
        m = re.search(r'(?:Owner|Wallet)[:\s]*<[^>]*>([^<]+)', html, re.DOTALL)
        if m:
            owner = m.group(1).strip()
        if not owner:
            m = re.search(r'[A-Za-z0-9_-]{48,}', html)
            if m:
                owner = m.group(0)

        result = {
            'username': clean,
            'status': status,
            'price_ton': price_ton,
            'owner': owner,
            'auction_end': auction_end,
            'min_bid': min_bid,
            'url': f'https://fragment.com/username/{clean}',
        }
        cache_set(cache_key, result, ttl=FRAGMENT_CACHE_TTL)
        return result
    except Exception as e:
        logger.error(f"Fragment lookup failed for {clean}: {e}")
        return None


def _fragment_collection_floor(slug):
    cache_key = f'frag_floor_{slug}'
    cached = cache_get(cache_key)
    if cached:
        return cached
    try:
        resp = requests.get(f'https://fragment.com/gifts/{slug}?filter=sale', timeout=10,
                            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        if resp.status_code != 200:
            return None
        html = resp.text
        floor = None
        total = None
        # All TON prices listed for this collection; floor = minimum
        prices = [float(m.group(1).replace(',', ''))
                  for m in re.finditer(r'tm-value[^>]*icon-ton[^>]*>([\d.,]+)', html)]
        if prices:
            floor = str(min(prices))
        # Collection total count from the sidebar filter
        m = re.search(rf'data-value=\"{slug}\"[^>]*>.*?tm-main-filters-count[^>]*>([\d.,]+)', html, re.DOTALL)
        if m:
            total = m.group(1).replace(',', '')
        result = {'slug': slug, 'floor_ton': floor, 'total': total}
        cache_set(cache_key, result, ttl=FRAGMENT_CACHE_TTL)
        return result
    except Exception as e:
        logger.error(f"Collection floor fetch failed for {slug}: {e}")
        return None


@bot.message_handler(commands=['fragment', 'username'])
@rate_limit_check
def fragment_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "Usage: /fragment @username\nLook up a Telegram username on Fragment.com")
        return
    target = args[1].strip()
    bot.send_message(message.chat.id, f"🔍 Looking up <b>{target}</b> on Fragment...", parse_mode='HTML')
    data = _fragment_username_data(target)
    if not data:
        bot.reply_to(message, "❌ Could not fetch data from Fragment. The username may not exist.")
        return
    lines = [f"🏷 <b>@{data['username']}</b>\n"]
    lines.append(f"📌 Status: <b>{data['status']}</b>")
    if data['min_bid']:
        ton_price = get_crypto_price('the-open-network')
        usd = float(data['min_bid']) * ton_price if ton_price else None
        usd_str = f" (${usd:,.2f})" if usd else ""
        lines.append(f"💰 Min bid: <b>{data['min_bid']} TON</b>{usd_str}")
    if data['price_ton'] and not data['min_bid']:
        ton_price = get_crypto_price('the-open-network')
        price_usd = float(data['price_ton']) * ton_price if ton_price else None
        usd_str = f" (${price_usd:,.2f})" if price_usd else ""
        lines.append(f"💰 Price: <b>{data['price_ton']} TON</b>{usd_str}")
    if data['auction_end']:
        lines.append(f"⏳ Ends: <b>{data['auction_end']}</b>")
    if data['owner']:
        addr = data['owner']
        lines.append(f"👤 Owner: <code>{addr[:8]}...{addr[-4:]}</code>")
    lines.append(f"🔗 <a href='{data['url']}'>View on Fragment</a>")
    bot.reply_to(message, add_timestamp("\n".join(lines)), parse_mode='HTML')


@bot.message_handler(commands=['gifts'])
@rate_limit_check
def gifts_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    lines = ["🎁 <b>Top Telegram Gift Collections</b>\n"]
    # Show top 5 by floor price
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        fut = {ex.submit(_fragment_collection_floor, c['slug']): c for c in FRAGMENT_GIFT_COLLECTIONS[:5]}
        for f in concurrent.futures.as_completed(fut):
            c = fut[f]
            data = f.result()
            floor = float(data['floor_ton']) if data and data.get('floor_ton') else None
            if floor:
                ton_price = get_crypto_price('the-open-network')
                usd = f" (${floor * ton_price:,.2f})" if ton_price else ""
                lines.append(f"• <b>{c['name']}</b> — <code>{floor} TON</code>{usd}")
            else:
                lines.append(f"• <b>{c['name']}</b> — <code>/gift {c['slug']}</code>")
    lines.append(f"\nAll {len(FRAGMENT_GIFT_COLLECTIONS)} collections available.")
    lines.append("Use /gift <i>slug</i> for details.")
    bot.reply_to(message, add_timestamp("\n".join(lines)), parse_mode='HTML')


@bot.message_handler(commands=['gift'])
@rate_limit_check
def gift_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "Usage: /gift collection_slug\nExample: /gift astralshard")
        return
    slug = args[1].strip().lower()
    match = next((c for c in FRAGMENT_GIFT_COLLECTIONS if c['slug'] == slug), None)
    if not match:
        bot.reply_to(message, f"❌ Collection <b>{slug}</b> not found. Use /gifts to see all.", parse_mode='HTML')
        return
    bot.send_message(message.chat.id, f"🔍 Fetching <b>{match['name']}</b> floor price...", parse_mode='HTML')
    floor = _fragment_collection_floor(slug)
    lines = [f"🎁 <b>{match['name']}</b>\n"]
    if floor and floor.get('floor_ton'):
        ton_price = get_crypto_price('the-open-network')
        usd = float(floor['floor_ton']) * ton_price if ton_price else None
        usd_str = f" (${usd:,.2f})" if usd else ""
        lines.append(f"💎 Floor: <b>{floor['floor_ton']} TON</b>{usd_str}")
    if floor and floor.get('total'):
        lines.append(f"📦 Total items: <b>{floor['total']}</b>")
    lines.append(f"🔗 <a href='https://fragment.com/gifts/{slug}'>View on Fragment</a>")
    bot.reply_to(message, add_timestamp("\n".join(lines)), parse_mode='HTML')
@bot.inline_handler(lambda query: len(query.query.strip()) > 0)
def inline_query_handler(inline_query):
    q     = inline_query.query.strip()
    ql    = q.lower().strip()
    uid   = inline_query.from_user.id
    results = []

    def article(id_, title, desc, text, html=False):
        # Add timestamp to inline results
        text_with_time = add_timestamp(text) if html else text
        return types.InlineQueryResultArticle(
            id=id_, title=title, description=desc,
            input_message_content=types.InputTextMessageContent(
                text_with_time, parse_mode='HTML' if html else None
            )
        )

    # ── 1. TRON tx hash (64 hex chars) OR tronscan link ──────────────
    tx_hash_match = re.match(r'^[A-Fa-f0-9]{64}$', q)
    tronscan_match = re.match(r'https?://tronscan\.org/#/transaction/([A-Fa-f0-9]{64})', q)
    
    tx_hash = None
    if tx_hash_match:
        tx_hash = q
    elif tronscan_match:
        tx_hash = tronscan_match.group(1)
    
    if tx_hash:
        try:
            tx = get_tron_transaction_details(tx_hash, uid)
            tronscan_link = f"https://tronscan.org/#/transaction/{tx_hash}"
            results.append(article(
                "txhash", "TRON Transaction", "Tap to share TX details",
                f"{tx}\n\n🔗 {tronscan_link}"
            ))
        except Exception:
            pass

    # ── 2. TRON wallet address (34 chars) ────────────────────────────
    elif re.match(r'^[A-Za-z0-9]{34}$', q) and is_valid_tron_address(q):
        try:
            bal = get_tron_wallet_trx(q, uid)
            short = f"{q[:8]}...{q[-6:]}"
            results.append(article(
                "wallet_addr", f"TRON Wallet {short}", bal,
                f"{EMOJIS['wallet']} <b>TRON Wallet</b>\n\n"
                f"<code>{q}</code>\n\n{bal}", html=True
            ))
        except Exception:
            results.append(article(
                "wallet_addr", f"TRON Wallet", q,
                f"{EMOJIS['wallet']} <code>{q}</code>", html=True
            ))

    # ── 3. TON tx hash OR tonviewer/tonscan link ───────────────────
    ton_tx_match = re.match(r'^[A-Fa-f0-9]{64}$', q)
    tonviewer_match = re.match(r'https?://tonviewer\.com/transaction/([A-Fa-f0-9]{64})', q)
    tonscan_match = re.match(r'https?://tonscan\.org/tx/([A-Fa-f0-9]{64})', q)
    ton_tx_hash = None
    if ton_tx_match:
        ton_tx_hash = q
    elif tonviewer_match:
        ton_tx_hash = tonviewer_match.group(1)
    elif tonscan_match:
        ton_tx_hash = tonscan_match.group(1)
    if ton_tx_hash:
        try:
            tx = get_ton_transaction_details(ton_tx_hash, uid)
            results.append(article(
                "ton_tx", "TON Transaction", "Tap to share TX details",
                tx
            ))
        except Exception:
            pass

    # ── 4. TON wallet address (48 chars, EQ/UQ) ────────────────────
    elif len(q) == 48 and q[:2] in ('EQ', 'UQ') and is_valid_ton_address(q):
        try:
            bal = get_ton_wallet_balance(q, uid)
            short = f"{q[:8]}...{q[-6:]}"
            results.append(article(
                "ton_wallet", f"TON Wallet {short}", bal,
                f"{EMOJIS['wallet']} <b>TON Wallet</b>\n\n"
                f"<code>{q}</code>\n\n{bal}", html=True
            ))
        except Exception:
            results.append(article(
                "ton_wallet", f"TON Wallet", q,
                f"{EMOJIS['wallet']} <code>{q}</code>", html=True
            ))

    else:
        irr = get_usd_to_irr()

        if irr is not None:
            # ── 4. USD / Toman rate ───────────────────────────────────────
            usd_m = re.match(
                r'^([\d.,۰-۹٬٫]+)?\s*(usd|\$|dollar)(\s+(to|to)\s*(toman|تومان|تومن|irr))?$',
                ql
            )
            if usd_m:
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
                results.append(article(
                    "usd_toman", lbl, desc,
                    f"{EMOJIS['money']} <b>{lbl}</b>\n\n{desc}", html=True
                ))

            # ── 5. Toman → USD ────────────────────────────────────────────
            toman_m = re.match(
                r'^([\d.,۰-۹٬٫]+)\s*(toman|تومان|تومن|ریال|irr)(\s+(to|به)\s*(usd|\$))?$',
                ql
            )
            if toman_m:
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
                    f"{EMOJIS['money']} <b>{lbl}</b>\n\n{desc}", html=True
                ))

        # ── 6. Conversion (10 btc to eth / 100 usd trx) — FASTER ─────
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
            
            if src and dst and src != dst:
                # Prevent stars conversions
                if not (src == 'telegram-stars' or dst == 'telegram-stars'):
                    # Initialize result_val
                    result_val = None
                
                # FASTER: Pre-fetch prices, don't call convert_amount (which re-fetches)
                if src in CRYPTO_LIST and dst in CRYPTO_LIST:
                    p_src = get_crypto_price(src)
                    p_dst = get_crypto_price(dst)
                    if p_src and p_dst:
                        result_val = (float(amt) * p_src) / p_dst
                elif src == 'usd' and dst in CRYPTO_LIST:
                    p_dst = get_crypto_price(dst)
                    if p_dst:
                        result_val = float(amt) / p_dst
                elif src in CRYPTO_LIST and dst == 'usd':
                    p_src = get_crypto_price(src)
                    if p_src:
                        result_val = float(amt) * p_src
                elif irr is not None and src == 'toman' and dst == 'usd':
                    result_val = float(amt) / irr
                elif irr is not None and src == 'usd' and dst == 'toman':
                    result_val = float(amt) * irr
                elif irr is not None and src == 'toman' and dst in CRYPTO_LIST:
                    p_dst = get_crypto_price(dst)
                    if p_dst:
                        result_val = (float(amt) / irr) / p_dst
                elif irr is not None and src in CRYPTO_LIST and dst == 'toman':
                    p_src = get_crypto_price(src)
                    if p_src:
                        result_val = (float(amt) * p_src) * irr
                
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
                    
                    desc = f"{amt_formatted} {src_sym} = {result_formatted} {dst_sym}"
                    results.append(article(
                        "convert", f"{src_sym} → {dst_sym}", desc,
                        f"{T(uid, 'inline_conv_header')}\n\n{desc}", html=True
                    ))


        # ── 7. Amount + single coin (10 trx / 0.5 btc / 10u) ─────────
        # Support both "10 trx" and "10trx" (no space), and "10u" for USDT
        amt_m = re.match(r'^(\d+(?:\.\d+)?)\s*(\w+)$', ql)
        if amt_m:
            amt = float(amt_m.group(1))
            cur = detect_currency(amt_m.group(2), check_u_alias=True)
            if cur and cur in CRYPTO_LIST:
                p = get_crypto_price(cur)
                if p:
                    v_usd = amt * p
                    v_irr = v_usd * irr
                    name  = _sym(cur)
                    toman_lbl = T(uid, 'toman_label')
                    desc  = f"${v_usd:,.2f} | {v_irr:,.0f} {toman_lbl}"
                    results.append(article(
                        "amt_coin", f"{amt:,.6g} {name}",
                        desc,
                        f"{EMOJIS['money']} <b>{amt:,.6g} {name}</b>\n\n"
                        f"💵 ${v_usd:,.2f}\n💰 {v_irr:,.0f} {toman_lbl}", html=True
                    ))

        # ── 8. Single crypto name (btc / eth / trx / u …) ────────────
        crypto = detect_currency(ql, check_u_alias=True)
        if crypto and crypto in CRYPTO_LIST:
            p = get_crypto_price(crypto)
            if p:
                p_irr = p * irr
                name  = CRYPTO_LIST[crypto]
                toman_lbl = T(uid, 'toman_label')
                results.append(article(
                    "crypto_price", f"{name} Price",
                    f"{fmt_price(p)} | {p_irr:,.0f} {toman_lbl}",
                    f"📊 <b>{name}</b>\n\n💵 {fmt_price(p)}\n💰 {p_irr:,.0f} {toman_lbl}",
                    html=True
                ))

        # ── 9. "price" keyword → full price list ─────────────────────
        if ql in ('price', 'prices', 'قیمت', 'قیمت‌ها'):
            try:
                ids = ','.join(CRYPTO_LIST.keys())
                resp = requests.get(
                    f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd",
                    timeout=10
                )
                prices = resp.json()
                lines = [f"{EMOJIS['chart']} <b>Crypto Prices</b>\n"]
                for code, cname in CRYPTO_LIST.items():
                    pr = prices.get(code, {}).get('usd')
                    if pr:
                        lines.append(f"{cname}\n💵 {fmt_price(pr)} | 💰 {pr*irr:,.0f} T\n")
                txt = "\n".join(lines)
                results.append(article(
                    "all_prices", "All Crypto Prices",
                    "Tap to share full price list", txt, html=True
                ))
            except Exception:
                pass

        # ── 10. Gold prices ────────────────────────────────────────────
        if ql in ('gold', 'gold price', 'طلا', 'قیمت طلا'):
            gold = get_gold_prices()
            if gold and 'xau' in gold:
                xau = gold['xau']
                lines = [T(uid, 'gold_global', xau=f"{xau:,.2f}")]
                if irr:
                    xau_irr = xau * irr
                    lines.append(f"\n💰 {xau_irr:,.0f} {T(uid, 'toman_label')}/oz")
                txt = "".join(lines)
                results.append(article("gold", "Gold Price", f"XAU/USD: ${xau:,.2f}", txt, html=True))

        # ── 11. Telegram Stars ─────────────────────────────────────────
        if ql in ('star', 'stars', 'telegram stars', 'استار', 'استارز', 'ستاره'):
            stars_price = get_crypto_price('telegram-stars')
            if stars_price:
                toman_lbl = T(uid, 'toman_label')
                lines = [f"⭐ <b>Telegram Stars</b>\n\n💵 ${stars_price:.3f} USD"]
                if irr:
                    stars_irr = stars_price * irr
                    lines.append(f"💰 {stars_irr:,.0f} {toman_lbl}")
                txt = "\n".join(lines)
                results.append(article("stars", "Telegram Stars", f"${stars_price:.3f}", txt, html=True))

        # ── 12. Market (Fear & Greed) ──────────────────────────────────
        if ql in ('market', 'fear', 'greed', 'fear and greed', 'fear & greed', 'بازار', 'ترس و طمع'):
            try:
                resp = requests.get("https://api.coingecko.com/api/v3/global", timeout=8)
                g = resp.json().get('data', {})
                mcap = g.get('total_market_cap', {}).get('usd', 0)
                vol = g.get('total_volume', {}).get('usd', 0)
                btc_dom = g.get('market_cap_percentage', {}).get('btc', 0)
                eth_dom = g.get('market_cap_percentage', {}).get('eth', 0)
                coins = g.get('active_cryptocurrencies', 0)
                chg24 = g.get('market_cap_change_percentage_24h_usd', 0)
                arrow = '📈' if chg24 >= 0 else '📉'
                lines = [f"🌍 <b>Crypto Market</b>\n"]
                lines.append(f"💹 Market Cap: <b>${mcap/1e12:.2f}T</b>  {arrow} {chg24:.1f}%")
                lines.append(f"📊 24h Volume: <b>${vol/1e12:.2f}T</b>")
                lines.append(f"🟠 BTC Dom: <b>{btc_dom:.1f}%</b>  🔵 ETH Dom: <b>{eth_dom:.1f}%</b>")
                lines.append(f"🪙 Active coins: <b>{coins}</b>")
                try:
                    fg_resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
                    fg_data = fg_resp.json().get('data', [{}])[0]
                    fg_val = int(fg_data.get('value', 50))
                    fg_label = fg_data.get('value_classification', '?')
                    filled = round(fg_val / 10)
                    bar = '🟢' * filled + '⬜' * (10 - filled)
                    lines.append(f"\n😨 <b>Fear & Greed</b>\n{bar}\n{fg_val}/100 — <b>{fg_label}</b>")
                except Exception:
                    pass
                txt = "\n".join(lines)
                results.append(article("market", "Market Overview", f"${mcap/1e12:.2f}T · {chg24:+.1f}%", txt, html=True))
            except Exception:
                pass

        # ── 13. TRY to Toman rate ──────────────────────────────────────
        if ql in ('try', 'tl', 'lira', 'ترک', 'لیر'):
            try_rate = get_try_to_irr()
            if try_rate:
                toman_lbl = T(uid, 'toman_label')
                txt = f"🇹🇷 <b>1 TRY = {try_rate} {toman_lbl}</b>"
                results.append(article("try_rate", f"1 TRY = {try_rate} {toman_lbl}", f"TRY → {toman_lbl}", txt, html=True))

        # ── 14. Holdings / Portfolio ────────────────────────────────────
        if ql in ('holdings', 'portfolio', 'port', 'پرتفو', 'دارایی'):
            saved = db_get_holdings(uid) or {}
            if saved:
                lines = [f"💼 <b>Portfolio</b>\n"]
                total = 0.0
                for symbol, amount in saved.items():
                    cid = detect_currency(symbol.lower())
                    if not cid:
                        continue
                    p = get_crypto_price(cid)
                    if p:
                        val = amount * p
                        total += val
                        lines.append(f"🪙 <b>{symbol}</b>: {fmt_price(val)}")
                    else:
                        lines.append(f"🪙 <b>{symbol}</b>: <i>N/A</i>")
                if irr:
                    total_irr = total * irr
                    lines.append(f"\n💰 <b>Total: {fmt_price(total)}</b> · {total_irr:,.0f} {T(uid, 'toman_label')}")
                else:
                    lines.append(f"\n💰 <b>Total: {fmt_price(total)}</b>")
                txt = "\n".join(lines)
                results.append(article("holdings", "Portfolio", f"Total: {fmt_price(total)}", txt, html=True))

        # ── 15. Alerts ──────────────────────────────────────────────────
        if ql in ('alerts', 'alert', 'my alerts', 'my alert', 'هشدار', 'هشدارها'):
            alerts = db_get_alerts(uid)
            if alerts:
                lines = [f"🔔 <b>Active Alerts</b>  ({len(alerts)})\n"]
                for a in alerts:
                    arrow = '📈' if a['direction'] == 'above' else '📉'
                    direction_word = T(uid, 'above_word') if a['direction'] == 'above' else T(uid, 'below_word')
                    lines.append(f"{arrow} {a['symbol']} {direction_word} {fmt_price(a['target_price'])}")
                txt = "\n".join(lines)
                results.append(article("alerts", f"Alerts ({len(alerts)})", f"{len(alerts)} active", txt, html=True))
            else:
                results.append(article("no_alerts", T(uid, 'no_alerts_simple'), "", T(uid, 'no_alerts_simple'), html=True))

        # ── 16. Math expression ───────────────────────────────────────
        if re.match(r'^[\d+\-*/().%\s]+$', q) and any(c in q for c in '+-*/'):
            res = evaluate_math(q)
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
                        # Address-only mode — instant, no API call
                        results.append(article(
                            f"waddr_{i}", f"📋 {wlbl}  {short}",
                            T(uid, 'wallet_addr_only_desc'),
                            f"{EMOJIS['wallet']} <b>{wlbl}</b>\n\n<code>{addr}</code>",
                            html=True
                        ))
                    else:
                        # Show both: address-only first, then with balance
                        results.append(article(
                            f"waddr_{i}", f"📋 {wlbl} — {T(uid, 'wallet_addr_only_label')}",
                            short,
                            f"{EMOJIS['wallet']} <b>{wlbl}</b>\n\n<code>{addr}</code>",
                            html=True
                        ))
                        try:
                            bal = get_tron_wallet_trx(addr, uid)
                            results.append(article(
                                f"wbal_{i}", f"👛 {wlbl} — {T(uid, 'wallet_with_balance_label')}",
                                bal,
                                f"{EMOJIS['wallet']} <b>{wlbl}</b>\n\n"
                                f"<code>{addr}</code>\n\n{bal}", html=True
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
    bot.answer_inline_query(
        inline_query.id, 
        results, 
        cache_time=1, 
        is_personal=True,
        switch_pm_text=T(uid, 'inline_help_button'),
        switch_pm_parameter='inline_help'
    )



# ─────────────────────────────────────────────
# Price alerts
# ─────────────────────────────────────────────


@bot.message_handler(commands=['alert'])
@rate_limit_check
def alert_cmd(message):
    user_id = message.from_user.id
    parts = message.text.strip().split()

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

    # No args — show coin picker
    existing = db_get_alerts(user_id)
    if len(existing) >= MAX_ALERTS_PER_USER:
        bot.reply_to(message, T(message.from_user.id, 'alert_limit', max=MAX_ALERTS_PER_USER), parse_mode='HTML')
        return

    coins = [c for c in CRYPTO_LIST.keys() if c != 'telegram-stars']
    rows = []
    for i in range(0, len(coins), 3):
        row = []
        for cid in coins[i:i+3]:
            sym = _sym(cid)
            row.append(types.InlineKeyboardButton(sym, callback_data=f"alrt1_{cid}"))
        rows.append(row)
    rows.append([types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="alrt_cancel")])

    msg = bot.reply_to(
        message,
        T(user_id, 'alert_step1'),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(rows)
    )
    register_panel_owner(msg.message_id, user_id)


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
    lines = [T(user_id, 'alerts_header', count=len(alerts), max=MAX_ALERTS_PER_USER)]
    for a in alerts:
        arrow  = '📈' if a['direction'] == 'above' else '📉'
        cur    = get_crypto_price(a['crypto_id'])
        dword  = above_w if a['direction'] == 'above' else below_w
        if cur:
            pct_str = f"{abs((a['target_price']-cur)/cur)*100:.1f}"
            dist = f"  <i>{T(user_id, 'away_pct', pct=pct_str)}</i>"
        else:
            dist = ""
        lines.append(f"{arrow} <b>{a['symbol']}</b> {dword} <b>{fmt_price(a['target_price'])}</b>{dist}")
        keyboard.append([types.InlineKeyboardButton(
            f"🗑  {a['symbol']} {dword} {fmt_price(a['target_price'])}",
            callback_data=f"alertdel_{a['id']}"
        )])
    keyboard.append([
        types.InlineKeyboardButton(T(user_id, 'btn_add_alert'),  callback_data="alrt_new"),
        types.InlineKeyboardButton(T(user_id, 'btn_delete_all'), callback_data="alertdelall"),
    ])
    bot.reply_to(
        message,
        "\n".join(lines),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(keyboard)
    )


@bot.message_handler(commands=['compare'])
@rate_limit_check
def compare_cmd(message):
    parts = message.text.strip().split()
    chart_mode = '--chart' in parts or '-c' in parts
    args = [p for p in parts if not p.startswith('-')]
    if len(args) >= 3 and chart_mode:
        bot.send_chat_action(message.chat.id, 'upload_photo')
        uid = message.from_user.id
        c1 = detect_currency(args[1])
        c2 = detect_currency(args[2])
        if not c1 or not c2:
            bot.reply_to(message, T(uid, 'unknown_coin'))
            return
        days = 30
        if len(args) >= 4:
            days = CHART_DAYS.get(args[3].lower(), 30)
        try:
            raw1 = _fetch_chart_data(c1, days)
            raw2 = _fetch_chart_data(c2, days)
            if not raw1 or not raw2:
                bot.reply_to(message, "❌ Failed to fetch chart data.")
                return
            dates1 = [datetime.fromtimestamp(p[0] / 1000) for p in raw1]
            prices1 = [p[1] for p in raw1]
            dates2 = [datetime.fromtimestamp(p[0] / 1000) for p in raw2]
            prices2 = [p[1] for p in raw2]
            fig, ax = plt.subplots(figsize=(6, 3.8))
            fig.patch.set_facecolor('#0e1117')
            ax.set_facecolor('#0e1117')
            ax.plot(dates1, prices1, color='#00cc96', linewidth=1.5, label=_sym(c1))
            ax.plot(dates2, prices2, color='#636efa', linewidth=1.5, label=_sym(c2))
            ax.grid(True, color='gray', linestyle='--', linewidth=0.3, alpha=0.3)
            ax.spines['top'].set_visible(False); ax.spines['right'].set_visible(False)
            ax.spines['left'].set_color('#555'); ax.spines['bottom'].set_color('#555')
            ax.tick_params(axis='x', colors='#aaa', labelsize=8, rotation=30)
            ax.tick_params(axis='y', colors='#aaa', labelsize=8)
            ax.set_title(f"{_sym(c1)} vs {_sym(c2)} — {days}d", color='#ccc', fontsize=11, pad=8)
            ax.legend(facecolor='#1a1a2e', edgecolor='none', labelcolor='white', fontsize=9)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
            buf = BytesIO()
            plt.savefig(buf, format='png', bbox_inches='tight', dpi=80,
                        facecolor=fig.get_facecolor(), edgecolor='none')
            buf.seek(0); plt.close(fig)
            p1 = get_crypto_price(c1); p2 = get_crypto_price(c2)
            caption = f"📊 <b>{_sym(c1)}</b> {fmt_price(p1) if p1 else '—'}  vs  <b>{_sym(c2)}</b> {fmt_price(p2) if p2 else '—'}  ({days}d)"
            bot.send_photo(message.chat.id, photo=BytesIO(buf.getvalue()),
                           caption=add_timestamp(caption), parse_mode='HTML')
            return
        except Exception as e:
            logger.error(f"Compare chart failed: {e}")
            bot.reply_to(message, "❌ Comparison chart failed. Try again later.")
            return
    if len(args) >= 3:
        _do_compare(message, args[1], args[2], message.from_user.id)
        return

    # No args — show coin picker
    coins = [c for c in CRYPTO_LIST.keys() if c != 'telegram-stars']
    rows = []
    for i in range(0, len(coins), 3):
        row = []
        for cid in coins[i:i+3]:
            sym = _sym(cid)
            row.append(types.InlineKeyboardButton(sym, callback_data=f"cmp1_{cid}"))
        rows.append(row)

    msg = bot.reply_to(
        message,
        T(message.from_user.id, 'compare_pick1'),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(rows)
    )
    # Register panel owner
    register_panel_owner(msg.message_id, message.from_user.id)


def _do_compare(message, raw1, raw2, user_id: int = 0, edit_msg_id=None):
    """
    Called from both /compare command and callback buttons.
    Uses send_message (not reply_to) so it works even when the
    original picker message has been deleted.
    Uses get_crypto_price() (cached) to avoid CoinGecko 429 errors.
    """
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, 'typing')

    ids, names = [], []
    for raw in (raw1, raw2):
        cid = detect_currency(raw.lower())
        if not cid:
            cid = raw.lower()
        if cid not in CRYPTO_LIST:
            uid_tmp = user_id or getattr(getattr(message, "from_user", None), "id", 0)
            bot.send_message(chat_id, T(uid_tmp, 'unknown_coin', sym=raw), parse_mode='HTML')
            return
        ids.append(cid)
        names.append(_sym(cid))

    usd_to_irr = get_usd_to_irr()
    cmp_uid = user_id or getattr(getattr(message, "from_user", None), "id", 0)
    # Use cached get_crypto_price — avoids separate CoinGecko call and 429s
    # For 24h change we do one batched call but fall back gracefully on 429
    change_data = {}
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={','.join(ids)}&vs_currencies=usd"
            f"&include_24hr_change=true&include_market_cap=true&include_24hr_vol=true",
            timeout=10
        )
        if resp.status_code == 200:
            change_data = resp.json()
    except Exception:
        pass  # fall back to price-only mode below

    blocks = []
    prices = []
    changes = []
    for cid, name in zip(ids, names):
        price  = get_crypto_price(cid) or 0
        prices.append(price)
        cd     = change_data.get(cid, {})
        change = cd.get('usd_24h_change')
        mcap   = cd.get('usd_market_cap', 0)
        vol    = cd.get('usd_24h_vol', 0)
        changes.append(change or 0)
        arrow  = '📈' if (change or 0) >= 0 else '📉'
        # Format change in English always (not Persian digits)
        chg    = f"{arrow} {change:+.2f}% (24h)" if change is not None else ""
        vol_str  = f"${vol/1e6:,.1f}M"  if vol  else "—"
        mcap_str = f"${mcap/1e9:,.2f}B" if mcap else "—"
        
        # Format IRR price in user's locale (may be Persian)
        irr_formatted = _fmt_number(f"{price * usd_to_irr:,.0f}", cmp_uid) if usd_to_irr else None
        
        blocks.append(
            f"{'━'*20}\n"
            f"🪙 <b>{name}</b>\n"
            f"💵 <b>{fmt_price(price)}</b>  {chg}\n"
            + (T(cmp_uid, 'price_toman_line', irr=irr_formatted) if irr_formatted else "")
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
    text = add_timestamp(T(cmp_uid, 'compare_header') + "\n\n".join(blocks) + f"\n{'━'*20}{verdict}")

    if edit_msg_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=edit_msg_id, parse_mode='HTML', reply_markup=kb)
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=kb)
    else:
        msg = bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=kb)
        register_panel_owner(msg.message_id, cmp_uid)


@bot.message_handler(commands=['market'])
@rate_limit_check
def market_cmd(message, user_id=None, edit_msg_id=None):
    uid_m = user_id or message.from_user.id
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        resp = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        resp.raise_for_status()
        g = resp.json().get('data', {})
    except Exception:
        bot.send_message(message.chat.id, T(uid_m, 'market_unavailable'))
        return

    try:
        fg_resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        fg_data = fg_resp.json().get('data', [{}])[0]
        fg_val   = int(fg_data.get('value', 50))
        fg_label_raw = fg_data.get('value_classification', '?')
        fg_label_map = {
            'Extreme Fear': T(uid_m, 'fg_extreme_fear'),
            'Fear':         T(uid_m, 'fg_fear'),
            'Neutral':      T(uid_m, 'fg_neutral'),
            'Greed':        T(uid_m, 'fg_greed'),
            'Extreme Greed':T(uid_m, 'fg_extreme_greed'),
        }
        fg_label = fg_label_map.get(fg_label_raw, fg_label_raw)
        filled = round(fg_val / 10)
        bar = '🟢' * filled + '⬜' * (10 - filled)
        fg_str = f"{bar}\n{fg_val}/100 — <b>{fg_label}</b>"
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
        T(uid_m, 'market_mcap', mcap=f"${mcap/1e12:.2f}T", arrow=arrow, chg=f"{chg24:+.1f}") +
        T(uid_m, 'market_vol',  vol=f"${vol/1e9:.1f}B") +
        T(uid_m, 'market_dom',  btc=f"{btc_dom:.1f}", eth=f"{eth_dom:.1f}") +
        T(uid_m, 'market_coins', coins=f"{coins:,}") +
        T(uid_m, 'market_fg', bar=fg_str)
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
        d = f" — {detail}" if detail else ""
        results.append(f"  ❌ {text}{d}")

    def section(title):
        results.append(f"\n━━━ {title} ━━━")

    # ── Database ──────────────────────────────────────────
    section("DATABASE")
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM user_languages")
            conn.close()
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
        resp = requests.get("https://api.coingecko.com/api/v3/ping", timeout=10)
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
            fail("Fetch BTC price — empty response")
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
        resp = requests.get("https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD", timeout=10)
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
                    fail(f"  {_sym(cid)} — not in response")
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
        results.append(f"\n  ⚠️  {failed} test(s) failed — check the ❌ entries above.")

    report = "🔬 <b>Bot Diagnostic Report</b>\n" + "\n".join(results)

    # Split long messages (Telegram 4096 limit)
    for chunk in [report[i:i+4000] for i in range(0, len(report), 4000)]:
        bot.send_message(chat_id, chunk, parse_mode='HTML')

@bot.message_handler(commands=['admin'])
@rate_limit_check
def admin_panel(message):
    global _cache
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "⛔ Owner only command.")
        return
    
    # Get bot statistics
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
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
        
        conn.close()
    
    # Get cache stats
    cache_size = len(_cache)
    
    kb = types.InlineKeyboardMarkup([
        [types.InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast")],
        [types.InlineKeyboardButton("🔄 Clear Cache", callback_data="admin_clear_cache")],
        [types.InlineKeyboardButton("📊 Detailed Stats", callback_data="admin_stats")]
    ])
    
    msg = (
        f"🔧 <b>Admin Panel</b>\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🔔 Active Alerts: {total_alerts}\n"
        f"💼 Holdings: {total_holdings}\n"
        f"👛 Tracked Wallets: {total_wallets}\n"
        f"🗂️ Cache Size: {cache_size} items\n\n"
        f"<i>Select an action below:</i>"
    )
    
    sent_msg = bot.reply_to(message, msg, parse_mode='HTML', reply_markup=kb)
    register_panel_owner(sent_msg.message_id, message.from_user.id)

# ─────────────────────────────────────────────
# General text handler  ← catch-all, must stay LAST
# ─────────────────────────────────────────────
@bot.message_handler(func=lambda message: True)
@rate_limit_check
def handle_text(message):
    user_id = message.from_user.id
    if not message.text:
        return  # ignore stickers, photos, voice messages, etc.
    text_original = message.text.strip()
    # Normalize Persian-Indic digits/operators → ASCII so all regex matches work
    text = _normalize_persian(text_original)
    text_lower = text.lower()

    # ── State machine ─────────────────────────
    state = get_user_state(user_id)

    # Suggestion forwarding (must be before admin broadcast)
    if state == 'awaiting_suggestion':
        del_user_state(user_id)
        if SUGGESTION_CHAT_ID:
            try:
                forward_text = (
                    f"💡 <b>New Suggestion</b>\n"
                    f"👤 User: {message.from_user.id}\n"
                    f"👤 Name: {message.from_user.full_name or message.from_user.first_name}\n"
                    f"💬 Message:\n{message.text}"
                )
                bot.send_message(SUGGESTION_CHAT_ID, forward_text, parse_mode='HTML')
                bot.reply_to(message, T(user_id, 'suggest_sent'))
            except Exception as e:
                logger.error(f"Failed to forward suggestion: {e}")
                bot.reply_to(message, T(user_id, 'suggest_fail'))
        else:
            bot.reply_to(message, T(user_id, 'suggest_fail'))
        return

    # Admin broadcast
    if state == 'admin_broadcast':
        if not is_owner(user_id):
            return
        
        del_user_state(user_id)
        
        # Get all users
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT DISTINCT user_id FROM user_languages")
            all_users = [row[0] for row in c.fetchall()]
            conn.close()
        
        broadcast_msg = message.text
        sent_count = 0
        failed_count = 0
        
        bot.reply_to(message, f"📢 Broadcasting to {len(all_users)} users...")
        
        for target_user in all_users:
            try:
                bot.send_message(target_user, broadcast_msg, parse_mode='HTML')
                sent_count += 1
                time.sleep(0.05)
            except Exception as e:
                failed_count += 1
                logger.error(f"Broadcast failed for user {target_user}: {e}")

        bot.send_message(
            message.chat.id,
            f"✅ <b>Broadcast Complete</b>\n\n"
            f"Sent: {sent_count}\n"
            f"Failed: {failed_count}",
            parse_mode='HTML'
        )
        return

    # Adding a wallet via the inline ➕ button
    if state == 'add_wallet_inline':
        del_user_state(user_id)
        _process_add_wallet(message, user_id, text.strip())
        return

    # Digest custom time input
    if state == 'digest_custom_hour':
        del_user_state(user_id)
        try:
            hour = int(text.strip())
            if not 0 <= hour <= 23:
                raise ValueError
        except ValueError:
            bot.reply_to(message, T(user_id, 'invalid_hour'))
            return
        # Default to 9 AM Iran time
        pref = db_get_digest(user_id) or {'enabled': False, 'hour': 9}
        db_set_digest(user_id, pref['enabled'], hour)
        bot.reply_to(
            message,
            T(user_id, 'digest_time_confirm', hour=f"{hour:02d}"),
            parse_mode='HTML',
            reply_markup=_build_digest_keyboard(pref['enabled'], hour, user_id)
        )
        return

    # Convert wizard step 3 — waiting for amount
    if state and state.startswith('convert_'):
        del_user_state(user_id)
        _, from_cid, to_cid = state.split('_', 2)
        from_sym = _sym(from_cid)
        to_sym   = _sym(to_cid)
        try:
            amount = float(text.strip().replace(',', ''))
        except ValueError:
            bot.reply_to(message, T(user_id, 'invalid_amount_conv'), parse_mode='HTML')
            return
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
            bot.reply_to(
                message,
                add_timestamp(
                    T(user_id, 'convert_result', amount=f"{amount:,g}", from_sym=from_sym,
                    result=result_str_w, to_sym=display_to_sym_w) + toman_note
                ),
                parse_mode='HTML'
            )
        return

    # Alert wizard step 3 — waiting for target price
    if state and state.startswith('alert_price_'):
        del_user_state(user_id)
        parts_s = state.split('_', 3)
        if len(parts_s) != 4:
            bot.reply_to(message, T(user_id, 'alert_state_error'))
            return
        _, _, cid, direction = parts_s
        _finalize_alert(message, user_id, cid, direction, text.strip())
        return

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
            return  # keep state so user can retry
        del_user_state(user_id)
        saved = db_get_holdings(user_id) or {}
        old_amt = saved.get(sym, 0)
        saved[sym] = old_amt + amount
        db_set_holdings(user_id, saved)
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        inner_kb = build_holdings_keyboard(saved, user_id)
        bot.reply_to(
            message,
            T(user_id, 'holding_added', sym=sym, amount=f"{amount:,g}", total=f"{saved[sym]:,g}") +
            holdings_message_text(saved, usd_to_irr, buy_prices, user_id),
            parse_mode='HTML',
            reply_markup=inner_kb
        )
        logger.info(f"User {user_id} updated holding: {sym}")
        return

    # Setting buy price for P&L
    if state and state.startswith('set_buy_price_'):
        symbol = state[len('set_buy_price_'):]
        try:
            buy_price = float(text.strip().replace(',', ''))
            if buy_price <= 0:
                raise ValueError("Price must be positive")
        except ValueError:
            bot.reply_to(message, T(user_id, 'invalid_price'))
            return
        db_set_buy_price(user_id, symbol, buy_price)
        del_user_state(user_id)
        saved = db_get_holdings(user_id) or {}
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        bot.reply_to(
            message,
            holdings_message_text(saved, usd_to_irr, buy_prices),
            parse_mode='HTML',
            reply_markup=build_holdings_keyboard(saved)
        )
        return

    # Editing an existing holding amount
    if state and state.startswith('edit_holding_'):
        symbol = state[len('edit_holding_'):]
        try:
            new_amount = float(text.strip().replace(',', ''))
            if new_amount < 0:
                raise ValueError
        except ValueError:
            bot.reply_to(message, T(user_id, 'invalid_amount'))
            return  # keep state so user can retry
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
        return

    # Math expressions — accepts ASCII and Persian-Indic digits/operators (normalized above)
    _has_pct_of = ('% of' in text_lower or '%of' in text_lower
                or '% از' in text_lower or '%از' in text_lower
                or '٪ از' in text_original or '٪از' in text_original)

    # ⚠️ FIX: Validate before attempting math evaluation
    # Must contain digits AND operators, but not ONLY operators
    has_digit = re.search(r'\d', text)
    has_operator = any(c in text for c in '+-*/%^')
    is_only_operator = text.strip() in ['+', '-', '*', '/', '%', '^', '(', ')', '+-', '--', '**', '//', '()', '^']

    if (re.match(r'^[\d+\-*/().%\s^]+$', text) and len(text) > 1
            and has_digit and has_operator and not is_only_operator) \
            or _has_pct_of:
        result = evaluate_math(text_original, user_id)
        bot.reply_to(message, result)
        return

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
            return
        result_toman = amount * Decimal(str(usd_to_irr))
        
        # Format with proper number handling
        user_lang = db_get_lang(user_id)
        amount_formatted = format_fiat(amount)
        result_formatted = format_fiat(result_toman, decimals=0)
        
        # Apply locale
        amount_formatted = format_for_locale(amount_formatted, user_lang)
        result_formatted = format_for_locale(result_formatted, user_lang)
        
        toman_label = "Toman" if user_lang == 'en' else "تومان"
        
        if amount == Decimal('1'):
            reply_text = f"1 USD = {result_formatted} {toman_label}"
        else:
            reply_text = f"{amount_formatted} USD = {result_formatted} {toman_label}"
        
        bot.reply_to(
            message,
            add_timestamp(f"💵 <b>{reply_text}</b>"),
            parse_mode='HTML'
        )
        return

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
            return
        result_toman = amount * Decimal(str(try_to_irr))

        user_lang = db_get_lang(user_id)
        amount_formatted = format_fiat(amount)
        result_formatted = format_fiat(result_toman, decimals=0)
        amount_formatted = format_for_locale(amount_formatted, user_lang)
        result_formatted = format_for_locale(result_formatted, user_lang)

        toman_label = "Toman" if user_lang == 'en' else "تومان"

        if amount == Decimal('1'):
            reply_text = f"1 TRY = {result_formatted} {toman_label}"
        else:
            reply_text = f"{amount_formatted} TRY = {result_formatted} {toman_label}"

        bot.reply_to(
            message,
            add_timestamp(f"💱 <b>{reply_text}</b>"),
            parse_mode='HTML'
        )
        return

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
            return

        gram_price = Decimal(str(xau_price)) / Decimal('31.1035')
        result_usd = amount * gram_price

        user_lang = db_get_lang(user_id)
        result_formatted = format_for_locale(format_fiat(result_usd), user_lang)
        amount_formatted = format_for_locale(format_fiat(amount), user_lang)
        reply_text = f"{amount_formatted}g Gold = ${result_formatted}"
        bot.reply_to(
            message,
            add_timestamp(f"🥇 <b>{reply_text}</b>"),
            parse_mode='HTML'
        )
        return

    # TRON wallet address
    if re.match(r'^[A-Za-z0-9]{34}$', text):
        if is_valid_tron_address(text):
            bot.send_chat_action(message.chat.id, 'typing')
            result = get_tron_wallet_trx(text, user_id)
            bot.reply_to(message, add_timestamp(result), parse_mode='HTML')
        else:
            bot.reply_to(message, T(user_id, 'invalid_tron_addr'))
        return

    # TON wallet address (48 chars, starts with EQ or UQ)
    if len(text) == 48 and text[:2] in ['EQ', 'UQ']:
        if is_valid_ton_address(text):
            bot.send_chat_action(message.chat.id, 'typing')
            result = get_ton_wallet_balance(text, user_id)
            bot.reply_to(message, add_timestamp(result), parse_mode='HTML')
            return

    # TON transaction hash or link (Tonscan / TonViewer)
    ton_tx_match = re.match(r'^[A-Fa-f0-9]{64}$', text)
    tonscan_match = re.match(r'https?://tonscan\.org/tx/([A-Fa-f0-9]{64})', text)
    tonviewer_match = re.match(r'https?://tonviewer\.com/transaction/([A-Fa-f0-9]{64})', text)

    if ton_tx_match or tonscan_match or tonviewer_match:
        tx_hash = text if ton_tx_match else (tonscan_match or tonviewer_match).group(1)
        
        bot.send_chat_action(message.chat.id, 'typing')
        
        # Try as TRON first (both use 64-char hex)
        tron_result = get_tron_transaction_details(tx_hash, user_id)
        if tron_result and "not found" not in tron_result.lower() and "error" not in tron_result.lower():
            bot.reply_to(message, add_timestamp(tron_result), parse_mode='HTML')
            return
        
        # If TRON failed, try TON
        ton_result = get_ton_transaction_details(tx_hash, user_id)
        bot.reply_to(message, add_timestamp(ton_result), parse_mode='HTML')
        return   

    # TRON transaction hash OR Tronscan link
    tx_hash_match = re.match(r'^[A-Fa-f0-9]{64}$', text)
    tronscan_match = re.match(r'https?://tronscan\.org/#/transaction/([A-Fa-f0-9]{64})', text)
    
    if tx_hash_match or tronscan_match:
        tx_hash = text if tx_hash_match else tronscan_match.group(1)
        bot.send_chat_action(message.chat.id, 'typing')
        result = get_tron_transaction_details(tx_hash, user_id)
        bot.reply_to(message, result, parse_mode='HTML')
        return

    # Standalone "try" → show TRY rate
    if text_lower in ('try', 'lira', 'tl', '₺') and not re.search(r'\d', text):
        bot.send_chat_action(message.chat.id, 'typing')
        try_to_irr = get_try_to_irr()
        if try_to_irr is None:
            bot.reply_to(message, add_timestamp(T(user_id, 'rate_unavailable')), parse_mode='HTML')
            return
        user_lang = db_get_lang(user_id)
        toman_label = "Toman" if user_lang == 'en' else "تومان"
        rate_formatted = format_for_locale(format_fiat(Decimal(str(try_to_irr)), decimals=0), user_lang)
        bot.reply_to(
            message,
            add_timestamp(T(user_id, 'try_rate', rate=rate_formatted)),
            parse_mode='HTML'
        )
        return

    # Standalone "gold" / "طلا" / "xau" → show gold price (global only)
    if text_lower in ('gold', 'طل', 'طلا', 'xau') and not re.search(r'\d', text):
        bot.send_chat_action(message.chat.id, 'typing')
        gold_prices = get_gold_prices()
        xau_price = gold_prices.get('xau')
        if not xau_price:
            bot.reply_to(message, T(user_id, 'price_fetch_fail'))
            return
        gram_price_usd = xau_price / 31.1035
        user_lang = db_get_lang(user_id)
        xau_formatted = format_for_locale(format_fiat(Decimal(str(xau_price))), user_lang)
        gram_usd_formatted = format_for_locale(format_fiat(Decimal(str(gram_price_usd))), user_lang)
        msg = (
            f"🥇 <b>Gold</b>\n\n"
            f"💵 XAU: <b>${xau_formatted}</b>\n"
            f"💵 1g: <b>${gram_usd_formatted}</b>"
        )
        bot.reply_to(message, add_timestamp(msg), parse_mode='HTML')
        return

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
            price_usd = None
            chart_bytes = None
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                price_fut = pool.submit(get_exchange_price, crypto, exch)
                chart_fut = pool.submit(get_crypto_chart_image, crypto, 30, user_id)
                price_usd = price_fut.result()
                try:
                    chart_result = chart_fut.result()
                    chart_bytes, symbol = chart_result
                except Exception:
                    chart_bytes = None

            usd_to_irr = cache_get('usd_to_irr') or get_usd_to_irr()
            if not price_usd:
                bot.reply_to(message, T(user_id, 'price_fetch_fail'))
                return
            toman_line = T(user_id, 'price_toman_line', irr=f"{price_usd * usd_to_irr:,.0f}") if usd_to_irr else ""

            if chart_bytes:
                bot.send_chat_action(message.chat.id, 'upload_photo')
                caption = add_timestamp(
                    f"📊 <b>{crypto_name}</b>\n\n"
                    f"💵 <b>{_fmt_price_with_exchange(price_usd, exch)}</b>\n"
                    + toman_line
                )
                bot.send_photo(
                    message.chat.id,
                    photo=BytesIO(chart_bytes),
                    caption=caption,
                    parse_mode='HTML',
                    reply_to_message_id=message.message_id,
                    reply_markup=refresh_kb
                )
            else:
                bot.reply_to(
                    message,
                    add_timestamp(
                        f"📊 <b>{crypto_name}</b>\n\n"
                        f"💵 <b>{_fmt_price_with_exchange(price_usd, exch)}</b>\n"
                        + toman_line
                    ),
                    parse_mode='HTML',
                    reply_markup=refresh_kb
                )
            return

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
                toman_line = f"🏦 {value_usd * usd_to_irr:,.0f} {T(user_id, 'toman_label')}" if usd_to_irr else ""
                bot.reply_to(
                    message,
                    add_timestamp(
                        f"💰 <b>{amount:,g} {sym}</b>\n\n"
                        f"💵 {fmt_price(value_usd)}\n"
                        + toman_line
                    ),
                    parse_mode='HTML'
                )
            else:
                bot.reply_to(message, T(user_id, 'price_fetch_fail'))
            return

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
                    bot.reply_to(
                        message,
                        add_timestamp(
                            T(user_id, 'convert_result', amount=f"{amount:,g}", from_sym=src_sym, result=result_str, to_sym=display_to_sym)
                        ),
                        parse_mode='HTML'
                    )
                else:
                    bot.reply_to(message, T(user_id, 'convert_fail', err=error_msg))
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
            lines = [T(user_id, 'alerts_header', count=len(alerts), max=MAX_ALERTS_PER_USER)]
            for a in alerts:
                arrow = '📈' if a['direction'] == 'above' else '📉'
                cur = get_crypto_price(a['crypto_id'])
                dword = above_w if a['direction'] == 'above' else below_w
                if cur:
                    pct_str = f"{abs((a['target_price']-cur)/cur)*100:.1f}"
                    dist = f"  <i>{T(user_id, 'away_pct', pct=pct_str)}</i>"
                else:
                    dist = ""
                lines.append(f"{arrow} <b>{a['symbol']}</b> {dword} <b>{fmt_price(a['target_price'])}</b>{dist}")
                keyboard.append([types.InlineKeyboardButton(
                    f"🗑  {a['symbol']} {dword} {fmt_price(a['target_price'])}",
                    callback_data=f"alertdel_{a['id']}"
                )])
            keyboard.append([
                types.InlineKeyboardButton(T(user_id, 'btn_add_alert'),  callback_data="alrt_new"),
                types.InlineKeyboardButton(T(user_id, 'btn_delete_all'), callback_data="alertdelall"),
            ])
            try:
                bot.edit_message_text(
                    "\n".join(lines),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    parse_mode='HTML',
                    reply_markup=types.InlineKeyboardMarkup(keyboard)
                )
            except Exception:
                pass
        bot.answer_callback_query(call.id, T(user_id, 'alert_deleted'))
        return True

    if data == "alertdelall":
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM alerts WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
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
        bot.answer_callback_query(call.id, T(user_id, 'alerts_all_deleted'))
        return True

    if data.startswith("digest_"):
        action = data[len("digest_"):]
        pref = db_get_digest(user_id) or {'enabled': False, 'hour': 9}
        if action == "on":
            db_set_digest(user_id, True, pref['hour'])
            bot.answer_callback_query(call.id, T(user_id, 'digest_on_toast'))
        elif action == "off":
            db_set_digest(user_id, False, pref['hour'])
            bot.answer_callback_query(call.id, T(user_id, 'digest_off_toast'))
        elif action == "custom":
            bot.answer_callback_query(call.id)
            set_user_state(user_id, 'digest_custom_hour')
            bot.send_message(
                call.message.chat.id,
                T(user_id, 'digest_time_prompt'),
                parse_mode='HTML'
            )
            return True
        elif action.startswith("h"):
            try:
                hour = int(action[1:])
                db_set_digest(user_id, pref['enabled'], hour)
                bot.answer_callback_query(call.id, T(user_id, 'digest_time_set', hour=f"{hour:02d}"))
            except ValueError:
                bot.answer_callback_query(call.id, T(user_id, 'invalid_hour'))
                return True
        # Refresh the digest message
        pref    = db_get_digest(user_id) or {'enabled': False, 'hour': 9}
        enabled = pref['enabled']
        hour    = pref['hour']
        status  = T(user_id, 'digest_enabled') if enabled else T(user_id, 'digest_disabled')
        try:
            bot.edit_message_text(
                T(user_id, 'digest_header') +
                T(user_id, 'digest_status', status=status, hour=f"{hour:02d}"),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=_build_digest_keyboard(enabled, hour, user_id)
            )
        except Exception:
            pass
        return True

    return False


# ─────────────────────────────────────────────
# Background: alert checker (runs every 60s)
# ─────────────────────────────────────────────
def _alert_checker_loop():
    logger.info("Alert checker thread started.")
    while True:
        try:
            alerts = db_get_all_alerts()
            if alerts:
                # ── Batch-fetch all needed prices in one CoinGecko call ───
                unique_ids = list({a['crypto_id'] for a in alerts})
                price_map: dict[str, float] = {}
                try:
                    resp = requests.get(
                        f"https://api.coingecko.com/api/v3/simple/price"
                        f"?ids={','.join(unique_ids)}&vs_currencies=usd",
                        timeout=10
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for cid in unique_ids:
                            p = data.get(cid, {}).get('usd')
                            if p:
                                price_map[cid] = p
                                cache_set(cid, p)
                except Exception as e:
                    logger.warning(f"Alert checker batch fetch failed: {e} — falling back to cache")
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
                            logger.info(
                                f"Alert {a['id']} fired for user {a['user_id']}: "
                                f"{a['symbol']} {a['direction']} {fmt_price(a['target_price'])}"
                            )
                            db_delete_alert_by_id(a['id'])
                        except Exception as e:
                            logger.error(f"Could not send alert to user {a['user_id']}: {e}")
        except Exception as e:
            logger.error(f"Alert checker error: {e}", exc_info=True)
        time.sleep(60)


# ─────────────────────────────────────────────
# Background: daily digest sender
# ─────────────────────────────────────────────
def _send_digest(user_id):
    try:
        saved = db_get_holdings(user_id)
        if not saved:
            return
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)

        # Fetch 24h change for each held coin
        crypto_ids = [detect_currency(s.lower()) for s in saved if detect_currency(s.lower())]
        change_map = {}
        if crypto_ids:
            try:
                url = (f"https://api.coingecko.com/api/v3/simple/price"
                       f"?ids={','.join(set(crypto_ids))}&vs_currencies=usd&include_24hr_change=true")
                resp = requests.get(url, timeout=10)
                data = resp.json()
                for cid in crypto_ids:
                    change_map[cid] = data.get(cid, {}).get('usd_24h_change')
            except Exception:
                pass

        total_usd = 0.0
        lines = [_T_cached(user_id, 'digest_morning')]
        for symbol, amount in saved.items():
            cid = detect_currency(symbol.lower())
            if not cid:
                continue
            price = get_crypto_price(cid)
            if not price:
                continue
            value = amount * price
            total_usd += value
            change = change_map.get(cid)
            change_str = ""
            if change is not None:
                arrow = '📈' if change >= 0 else '📉'
                change_str = f" {arrow} {change:+.2f}%"
            pnl_str = ""
            buy = buy_prices.get(symbol.upper())
            if buy and buy > 0:
                pnl_usd = (price - buy) * amount
                sign = "+" if pnl_usd >= 0 else ""
                pnl_usd_abs = abs(pnl_usd)
                pnl_str = f" | P&L: {sign}{fmt_price(pnl_usd_abs)}"
            lines.append(f"🪙 <b>{symbol}</b>: {fmt_price(value)}{change_str}{pnl_str}")

        if usd_to_irr:
            total_irr = total_usd * usd_to_irr
            lines.append(_T_cached(user_id, 'digest_total', usd=fmt_price(total_usd), irr=f"{total_irr:,.0f}"))
        else:
            lines.append(_T_cached(user_id, 'digest_total', usd=fmt_price(total_usd), irr="N/A"))
        lines.append(f"\n<i>📅 {datetime.utcnow().strftime('%b %d, %Y  %H:%M UTC')}</i>")
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(_T_cached(user_id, 'btn_portfolio'), callback_data="show_holdings"),
            types.InlineKeyboardButton(_T_cached(user_id, 'btn_alerts'),    callback_data="show_alerts"),
        ]])
        bot.send_message(user_id, "\n".join(lines), parse_mode='HTML', reply_markup=kb)
    except Exception as e:
        logger.error(f"Digest send failed for user {user_id}: {e}")


def _digest_loop():
    logger.info("Daily digest thread started.")
    last_sent: dict[int, int] = {}  # user_id → last hour sent
    while True:
        try:
            now_hour = datetime.now(IRAN_TZ).hour
            users = db_get_all_digest_users()
            for u in users:
                uid, hour = u['user_id'], u['hour']
                if now_hour == hour and last_sent.get(uid) != now_hour:
                    threading.Thread(target=_send_digest, args=(uid,), daemon=True).start()
                    last_sent[uid] = now_hour
        except Exception as e:
            logger.error(f"Digest loop error: {e}")
        time.sleep(60)


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


# ─────────────────────────────────────────────
# Entry point — crash recovery polling loop
# Restarts automatically on network errors or
# unexpected crashes, with exponential back-off.
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info(f"{EMOJIS['rocket']} Crypto Price Bot Starting...")
    logger.info(f"{EMOJIS['info']} Bot: @{bot.get_me().username}")
    logger.info(f"{EMOJIS['check']} Status: Running")
    logger.info(f"{EMOJIS['chart']} Cache timeout: {CACHE_TIMEOUT}s")
    logger.info(f"{EMOJIS['info']} Rate limit: {USER_RATE_LIMIT} req / {USER_RATE_WINDOW}s per user")
    logger.info(f"{EMOJIS['info']} Max wallets per user: {MAX_WALLETS_PER_USER}")
    if not SAFE_EVAL_AVAILABLE:
        logger.warning("⚠️  simpleeval not installed — math uses restricted eval fallback. "
                       "Install it: pip install simpleeval")

    # Start background threads
    threading.Thread(target=_alert_checker_loop, daemon=True, name="AlertChecker").start()
    threading.Thread(target=_digest_loop, daemon=True, name="DigestSender").start()
    threading.Thread(target=cache_cleanup_loop, daemon=True, name="CacheCleanup").start()
    threading.Thread(target=_cleanup_user_state_loop, daemon=True, name="UserStateCleanup").start()
    threading.Thread(target=_prewarm_charts, daemon=True, name="ChartPrewarm").start()
    logger.info(f"{EMOJIS['check']} Background threads started (alerts, digest, cache cleanup, state cleanup, chart pre-warm)")

    logger.info(f"{EMOJIS['star']} Press Ctrl+C to stop")
    logger.info("=" * 50)

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
