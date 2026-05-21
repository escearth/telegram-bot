# 🌍 Earth Crypto — Telegram Bot

A fast, interactive Telegram bot for crypto prices, portfolio tracking, price alerts, currency conversion, and TRON wallet lookups — with full Persian (Farsi) input support.

**Bot username:** `@EscEarthBot`

---

## ✨ Features

| Feature | Details |
|---|---|
| 📊 **Live Prices** | 12 coins with 24h change — one tap to refresh |
| 💼 **Portfolio** | Track holdings with live P&L and buy-price tracking |
| 🔔 **Price Alerts** | Get notified when any coin hits your target |
| 💱 **Converter** | Convert between any crypto, USD, and Toman |
| 🌍 **Market Overview** | Total market cap, volume, BTC/ETH dominance, Fear & Greed index |
| 📈 **Coin Charts** | 30-day price charts generated on demand |
| 👛 **TRON Wallets** | Save wallet addresses, check TRX balance, look up transactions |
| ☀️ **Daily Digest** | Scheduled morning summary of your portfolio |
| 🧮 **Calculator** | Inline math, including `% of` expressions |
| 📲 **Inline Mode** | Use `@EscEarthBot` in any chat to share prices |
| 🔒 **Privacy** | `/privacy` and `/deleteaccount` for full GDPR compliance |

### Supported Coins

Bitcoin (BTC), Ethereum (ETH), Tether (USDT), BNB, Cardano (ADA), XRP, Solana (SOL), Polkadot (DOT), Dogecoin (DOGE), Shiba Inu (SHIB), Tron (TRX), Toncoin (TON)

### Quick-type (no command needed)

```
btc              → price + 30-day chart
10 trx           → value in USD & Toman
0.01 btc to eth  → instant conversion
150 usd          → USD → Toman rate
10+20*3          → calculator
TXabc...         → TRON wallet balance
<64-char hex>    → TRON transaction details
```

Persian input is supported for all coin names and currencies (e.g. `بیتکوین`, `تومان`, `دلار`).

---

## 🚀 Setup

### Prerequisites

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/earth-crypto-bot.git
cd earth-crypto-bot
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate        # Linux/macOS
venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
nano .env   # or use any text editor
```

Fill in your values:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ Yes | From @BotFather |
| `OWNER_USER_ID` | Optional | Your Telegram user ID — receive alerts when the USD/IRR API fails |
| `FALLBACK_USD_TO_IRR` | Optional | Fallback Toman rate when APIs are down (default: 750,000) |

> **Find your Telegram user ID:** message [@userinfobot](https://t.me/userinfobot) on Telegram.

### 5. Run the bot

```bash
python bot.py
```

The bot will start polling and print a startup banner. Press `Ctrl+C` to stop.

---

## 🗂️ Project Structure

```
earth-crypto-bot/
├── bot.py              # Main bot — all handlers, commands, callbacks
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── .gitignore          # Files excluded from git
└── README.md           # This file
```

### Runtime files (auto-created, not committed)

```
bot_data.db     # SQLite database — all user data lives here
bot.log         # Log file — rotate or truncate periodically
```

---

## ⚙️ Configuration

All tunable constants are near the top of `bot.py`:

| Constant | Default | Description |
|---|---|---|
| `USER_RATE_LIMIT` | `10` | Max requests per user per window |
| `USER_RATE_WINDOW` | `60` | Rate-limit window in seconds |
| `MAX_WALLETS_PER_USER` | `10` | Max saved TRON wallets per user |
| `MAX_ALERTS_PER_USER` | `10` | Max active price alerts per user |
| `CACHE_TIMEOUT` | `300` | Price cache TTL in seconds |
| `API_COOLDOWN` | `1.2` | Minimum seconds between CoinGecko calls |
| `FALLBACK_USD_TO_IRR` | env var | Toman rate when APIs fail |

---

## 🏗️ Architecture

```
bot.py
├── Database layer       SQLite via db_lock (thread-safe)
├── Cache layer          In-memory dict with TTL
├── API helpers          CoinGecko + CryptoCompare fallback
│                        Nobitex for USD/IRR rate
│                        TronScan for wallet/tx data
├── Command handlers     /start /price /market /holdings /set
│                        /alert /alerts /compare /convert
│                        /digest /wallets /mywallets /usd
│                        /privacy /deleteaccount /cancel
├── Callback handler     All inline button interactions
├── Inline handler       @EscEarthBot inline queries
├── Text catch-all       Direct typing (btc, 10 trx, math, etc.)
└── Background threads   AlertChecker (60s), DigestSender (60s)
```

**Price sources (in order of preference):**

1. CoinGecko API (cached for 5 min)
2. CryptoCompare API (fallback)
3. In-memory cache (if both APIs fail)

**USD/IRR rate sources:**

1. Nobitex.ir market API (latest price)
2. Nobitex.ir best buy/sell average
3. `FALLBACK_USD_TO_IRR` env var (last resort)

---

## 🔒 Privacy & GDPR

The bot stores the following data per Telegram user ID:

- Portfolio holdings and buy prices
- Saved TRON wallet addresses (masked in logs)
- Price alert targets
- Daily digest preference

Users can view the full policy with `/privacy` and permanently delete all their data with `/deleteaccount`. No data is shared with or sold to third parties.

---

## 🚢 Production Deployment

### systemd service (Linux)

Create `/etc/systemd/system/earthcryptobot.service`:

```ini
[Unit]
Description=Earth Crypto Telegram Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/earth-crypto-bot
EnvironmentFile=/home/youruser/earth-crypto-bot/.env
ExecStart=/home/youruser/earth-crypto-bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable earthcryptobot
sudo systemctl start earthcryptobot
sudo systemctl status earthcryptobot
```

### View logs

```bash
sudo journalctl -u earthcryptobot -f      # systemd logs
tail -f bot.log                           # bot's own log file
```

### Database backup

```bash
# Backup
sqlite3 bot_data.db ".backup backup_$(date +%Y%m%d).db"

# Restore
cp backup_20260101.db bot_data.db
```

---

## 📡 APIs Used

| API | Purpose | Rate Limits |
|---|---|---|
| [CoinGecko](https://www.coingecko.com/en/api) | Crypto prices, charts, market data | 30 calls/min (free) |
| [CryptoCompare](https://min-api.cryptocompare.com/) | Fallback prices | 100 calls/min (free) |
| [Nobitex](https://apiv2.nobitex.ir/) | USD/IRR (Toman) rate | Public |
| [TronScan](https://tronscan.org/) | TRON wallet balances & transactions | Public |
| [Alternative.me](https://alternative.me/crypto/fear-and-greed-index/) | Fear & Greed Index | Public |

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m "Add my feature"`
4. Push: `git push origin feature/my-feature`
5. Open a Pull Request

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## ⚠️ Disclaimer

This bot is for informational purposes only. Crypto prices are volatile. Nothing here constitutes financial advice. Always do your own research before making investment decisions.
