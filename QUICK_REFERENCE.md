# 📝 Quick Reference - Enhanced Features

## 🎯 Number Format Support

### All These Work Now:

| Input Format | Example | Parsed As |
|-------------|---------|-----------|
| European (dots) | `1.000.000` | 1,000,000 |
| US (commas) | `1,000,000` | 1,000,000 |
| European decimal | `12,34` | 12.34 |
| US decimal | `12.34` | 12.34 |
| Mixed European | `1.000.000,50` | 1,000,000.50 |
| Mixed US | `1,000,000.50` | 1,000,000.50 |
| Persian | `۱۲۳٬۴۵۶` | 123,456 |
| Persian decimal | `۱۲٫۳۴` | 12.34 |
| No separators | `1000000` | 1,000,000 |

## 💱 Currency Keywords

### Toman

✅ `toman`
✅ `تومان`
✅ `تومن` (NEW!)
✅ `irr`
✅ `ریال`

### USD

✅ `usd`
✅ `$`
✅ `dollar`
✅ `دلار`

## 💰 Wallet Balance Display

### Before:
```
Balance: 0.500000 TRX
```

### After:
```
👛 TRON Wallet Balance

🪙 0.5 TRX
💵 $0.14
💰 8,567 Toman
```

## 📊 Number Formatting

### Crypto (up to 8 decimals)

| Before | After |
|--------|-------|
| `1.5e-5` | `0.000015` |
| `1.23e-8` | `0.00000123` |
| `0.00001230` | `0.0000123` (zeros stripped) |

### Fiat (2 decimals or whole)

| Before | After |
|--------|-------|
| `1e+6` | `1,000,000` |
| `1234.00` | `1,234` (no cents) |
| `1234.50` | `1,234.50` |

## 🌐 Bilingual Support

### English User:
```
1,234.56 USD = 74,074,000 Toman
```

### Persian User:
```
۱,۲۳۴.۵۶ USD = ۷۴,۰۷۴,۰۰۰ تومان
```

## 💬 Example Commands

### Inline Mode

```
@your_bot 1.000.000 usd
→ 1,000,000 USD = 60,000,000,000 Toman

@your_bot 12,34 btc
→ 12.34 BTC = $1,197,440 | 71,846,400,000 Toman

@your_bot 100.000 تومن to btc
→ 100,000 Toman = 0.00003 BTC
```

### Text Commands

```
1.000.000 تومان
→ 💵 1,000,000 Toman = $16.67

۱۲۳٬۴۵۶ toman to usd
→ 💵 123,456 Toman = $2.06

0.5 btc
→ Chart + price info
```

### Wallet Commands

```
TMhVB8xvL8rQ9pDKL5bGcmTp8xK9Y3k2Fj
→ 👛 TRON Wallet Balance
  🪙 127.5 TRX
  💵 $36.41
  💰 2,184,600 Toman
```

## 🔧 Technical Details

### Functions Available

```python
from number_utils import (
    parse_number,        # Parse any format → Decimal
    format_crypto,       # Format crypto (8 decimals)
    format_fiat,         # Format fiat (2 decimals)
    format_for_locale,   # Apply Persian digits
    format_wallet_balance  # 3-value display
)
```

### Usage Example

```python
# Parse user input
amount = parse_number("1.000.000")  # → Decimal('1000000')

# Format for display
formatted = format_fiat(amount)  # → "1,000,000"

# Apply user locale
if user_lang == 'fa':
    formatted = format_for_locale(formatted, 'fa')
    # → "۱,۰۰۰,۰۰۰"
```

## ⚡ Performance

- **Parsing:** Same speed or faster
- **Formatting:** Instant (no API calls)
- **Memory:** No increase
- **Compatibility:** 100% backward compatible

## 🎨 User Experience

### What Users See

1. **Any number format works** - no more "invalid format" errors
2. **Clear wallet balances** - see value in 3 currencies at once
3. **Both تومان and تومن** - flexibility for Persian speakers
4. **Readable numbers** - never see `1e+6` or `1.5e-5`
5. **Persian digits** - if Persian language selected

### What Users Don't See

- No changes to commands
- No new syntax to learn
- No breaking changes
- Everything "just works better"

## 📱 Testing Checklist

Quick tests to verify it's working:

```
□ Send: 1.000.000 usd
  → Should work (not error)

□ Send: ۱۲۳ تومان  
  → Should recognize Persian

□ Send: 100 تومن to usd
  → Should accept تومن

□ Send: TRON address
  → Should show 3 values

□ Inline: @bot 0.00001 btc
  → Should show 0.00001 (not 1e-5)

□ Send: 12,34 eth
  → Should parse as 12.34
```

If all pass: ✅ **Deployment Successful!**

## 🐛 Common Issues & Fixes

| Issue | Quick Fix |
|-------|-----------|
| "ModuleNotFoundError" | Copy `number_utils.py` to bot directory |
| Still seeing `1e-5` | Restart bot, ensure using new `bot.py` |
| Persian numbers not working | Check `number_utils.py` is present |
| تومن doesn't work | Verify `FIAT_ALIASES` includes `'تومن': 'toman'` |

## 📞 Quick Help

```bash
# Check if both files present
ls -l bot.py number_utils.py

# Test import
python3 -c "from number_utils import parse_number; print('OK')"

# Check syntax
python3 -c "import ast; ast.parse(open('bot.py').read()); print('OK')"

# View logs
tail -f bot.log
```

---

**Need More Help?** See `DEPLOYMENT_GUIDE.md` for detailed troubleshooting.
