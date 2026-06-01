# Changelog

All notable changes to Earth Crypto Bot are documented here.

---

## [2.0.1] — 2026-06-01

### Added
- **`_sym(cid)` helper** — central ticker-symbol extraction function, replaces 29+ inline `split('(')[1].replace(...)` calls
- **`_fetch_prices_batch()`** — rate-limited CoinGecko API wrapper with `@rate_limited_api_call`, used in `/price` command and `refresh_all_prices` callback to prevent 429 errors
- **`import ast`** — top-level import for secure math evaluation fallback
- **`/test` command** — owner-only diagnostic that tests all features (DB, APIs, parsing, math, addresses, conversion) and returns a combined report
- **`/admin` command** added to BotFather command list
- **Refresh rate limiter** — `is_refresh_allowed()` with 10s cooldown and 30 refreshes/hour per user; owner exempt
- **Colored inline buttons** — `style="danger"` (red) for delete/cancel, `style="success"` (green) for add/enable/confirm, `style="primary"` (blue) for navigation/refresh on all 69 inline keyboard buttons

### Changed
- **`evaluate_math`** now uses `simpleeval` when available, or falls back to `ast.parse` + restricted globals instead of bare `eval()`
- **`MAX_ALERTS_PER_USER`** moved from inline (line ~3583) to constants section next to `MAX_WALLETS_PER_USER`
- **Timestamps restored** on all refresh/callback outputs — `refresh_all_prices`, `refresh_{crypto}`, `market_refresh`, `cmpref_` now show `🕒 updated at HH:MM`
- **Timestamps removed** from non-essential messages — start welcome, cancel, errors, wizard steps, admin panel no longer show timestamp
- **`parse_number`** now handles thousand separators (`1.000.000`, `1,000,000`)
- **`pyTelegramBotAPI`** minimum bumped to `>=4.31.0` (required for button `style` support)

### Fixed
- **Missing translation key** — `btn_price` → `btn_buy_price` in holdings keyboard to prevent `KeyError`
- **Digest timezone** — `datetime.now().hour` → `datetime.now(IRAN_TZ).hour` in digest scheduler loop
- **Duplicate `user_state = {}`** declaration removed
- **Duplicate log line** `"User {id} requested prices"` removed
- **Corrupting global replacements** — reversed over-aggressive `_cid`→`_sym(cid)` / `_cur`→`_sym(cur)` / `_crypto`→`_sym(crypto)` refactors that broke `from_cid`, `detect_currency`, `get_crypto_price`, `format_crypto`, `p_src`/`p_dst`
- **`/test` expectations** — fixed `evaluate_math` and TRON address test assertions
- **`refresh_{crypto}`** — replaced manual `updated_at` string with `add_timestamp()` for consistent formatting

## [2.0.0] — 2026-02-15

### Added
- **Interactive button flows** for all commands — `/set`, `/convert`, `/alert`, `/compare`, `/holdings` all use inline coin pickers instead of text input
- **`/privacy` command** — full data disclosure (GDPR)
- **`/deleteaccount` command** — one-tap permanent data deletion
- **24h change indicators** on `/price` list (📈/📉 with %)
- **Fear & Greed bar** on `/market` (visual emoji bar 0–100)
- **`➕ Add to Holdings`** button on every coin chart
- **Daily digest quick actions** — Portfolio and Alerts buttons on morning message
- **Alert action buttons** — "Set New Alert" and "Holdings" on alert notifications
- **`OWNER_USER_ID` env var** — owner receives a message when the USD/IRR API fails
- **`FALLBACK_USD_TO_IRR` env var** — override fallback rate without code changes
- **Refresh buttons** on `/price` and `/market`
- **Distance indicator** on alerts list (e.g. "3.2% away")

### Changed
- `FALLBACK_USD_TO_IRR` updated from `62,000` to `750,000` to reflect 2026 rates
- Alert checker now batches all coin prices into **one CoinGecko API call** per cycle (was N calls)
- `hashlib` moved to top-level imports (was imported inside a function on every call)
- `rate_limited_api_call` lock released **before** HTTP I/O (was holding lock for full request duration)
- Portfolio header emoji: `👛` → `💼`
- Wallet list header: `👛 Your Saved Wallets:` → `👛 Your Saved Wallets`
- All price displays use `fmt_price()` — no more scientific notation or incorrect decimals for SHIB/DOGE
- Daily digest greeting: formal → `☀️ Good morning! Here's your portfolio`
- Date format in digest: `2026-02-15 08:00` → `Feb 15, 2026  08:00 UTC`
- Rate-limit message: cold → `⏳ Slow down a little! Try again in a moment.`

### Fixed
- **`edit_holding_` handler** was missing `buy_prices` argument — P&L disappeared after editing
- **Dead `add_holding_inline` state handler** removed (was never triggered, would have crashed with `TypeError`)
- **`_b58decode`** now raises `ValueError` cleanly on invalid Base58 characters (was `ValueError` inside `except Exception` gap)
- **`handle_text`** now guards against `None` message text (stickers/photos no longer crash the handler)
- **`alert_price_` state split** is now guarded against malformed state strings
- **`alertdelall`** now shows a `➕ Set Alert` button after wiping all alerts
- **`show_wallets` callback** no longer passes `keyboard=None` for empty wallet lists
- **Double `answer_callback_query`** on `hnoop_` removed
- **Wallet addresses** in logs are now masked (`TXabc1…ef12`)
- Clear All Holdings now uses inline confirm buttons instead of asking user to type "yes"

---

## [1.0.0] — 2026-02-13

### Initial release
- Live prices for 12 cryptocurrencies
- Portfolio tracking with P&L
- Price alerts with background checker
- USD/Toman conversion
- TRON wallet balance and transaction lookup
- Daily digest with scheduled delivery
- Inline mode for all features
- SQLite persistence
- Thread-safe caching and rate limiting
- Crash-recovery polling loop
