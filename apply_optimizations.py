import re

with open('bot.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Replace requests.get with session.get globally
content = content.replace("requests.get(", "session.get(")

# 2. Add the requests.Session pool
session_code = """
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Global highly-optimized session
session = requests.Session()
retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
session.mount('http://', adapter)
session.mount('https://', adapter)
"""

# Replace 'import requests' with the session init
content = content.replace("import requests", session_code, 1)

# 3. Add headless matplotlib and GC
matplotlib_imports = """
import matplotlib
matplotlib.use('Agg') # Extremely important for server-side memory
import matplotlib.pyplot as plt
import gc
"""
content = content.replace("import matplotlib.pyplot as plt", matplotlib_imports, 1)

# 4. Consolidate threads
# Find start_bot definition
start_bot_def = """def start_bot(start_web=True):"""
background_scheduler = """
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
        _prewarm_charts()
        get_usd_to_irr()
    except Exception as e:
        logger.error(f"Initial pre-warm failed: {e}")

    while True:
        now = time.time()
        
        # 1. Alert Checker (every 60s)
        if now - last_alert_check >= 60:
            last_alert_check = now
            try:
                # inline logic from _alert_checker_loop
                alerts = db_get_all_alerts()
                if alerts:
                    unique_ids = list({a['crypto_id'] for a in alerts})
                    price_map = {}
                    try:
                        resp = session.get(f"https://api.coingecko.com/api/v3/simple/price?ids={','.join(unique_ids)}&vs_currencies=usd", timeout=10)
                        if resp.status_code == 200:
                            price_map = {k: v.get('usd') for k, v in resp.json().items()}
                    except Exception as e:
                        logger.error(f"Alert price fetch error: {e}")

                    for a in alerts:
                        current_p = price_map.get(a['crypto_id'])
                        if current_p is not None:
                            _check_alert_condition(a, current_p)
            except Exception as e:
                logger.error(f"Alert checker loop error: {e}")

        # 2. WebApp Cache Warmer (every 300s)
        if now - last_webapp_cache >= 300:
            last_webapp_cache = now
            try:
                ids = ','.join(k for k in CRYPTO_LIST if k != 'telegram-stars')
                _fetch_prices_batch(ids)
                _webapp_sparklines(CRYPTO_LIST)
                _fetch_coingecko_global()
                get_usd_to_irr()
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
                tehran_tz = pytz.timezone("Asia/Tehran")
                now_t = datetime.now(tehran_tz)
                if now_t.hour == 8 and now_t.minute == 0:
                    # Not calling send_daily_digest directly because it doesn't exist, we must use _digest_loop logic
                    users = db_get_all_digest_users()
                    for uid in users:
                        try:
                            _send_digest_to_user(uid)
                        except Exception:
                            pass
            except Exception as e:
                pass

        # 6. Garbage Collection (every 900s)
        if now - last_gc >= 900:
            last_gc = now
            gc.collect()

        time.sleep(10) # Base tick rate

"""

content = content.replace(start_bot_def, background_scheduler + start_bot_def)

# Remove old threads from start_bot
old_threads = """    threading.Thread(target=_alert_checker_loop, daemon=True, name="AlertChecker").start()
    threading.Thread(target=_digest_loop, daemon=True, name="DigestSender").start()
    threading.Thread(target=cache_cleanup_loop, daemon=True, name="CacheCleanup").start()
    threading.Thread(target=_cleanup_user_state_loop, daemon=True, name="UserStateCleanup").start()
    threading.Thread(target=_prewarm_charts, daemon=True, name="ChartPrewarm").start()
    threading.Thread(target=_prewarm_prices, daemon=True, name="PricePrewarm").start()
    threading.Thread(target=lambda: get_usd_to_irr(), daemon=True, name="IRRPreWarm").start()
    threading.Thread(target=_webapp_cache_warmer_loop, daemon=True, name="WebappCacheWarmer").start()
    logger.info(f"{EMOJIS['check']} Background threads started (alerts, digest, cache cleanup, state cleanup, chart pre-warm, price pre-warm, IRR pre-warm, webapp cache warmer)")"""

new_threads = """    threading.Thread(target=_background_scheduler_loop, daemon=True, name="Scheduler").start()
    logger.info(f"{EMOJIS['check']} Universal background scheduler started (replaces 8 legacy threads)")"""

content = content.replace(old_threads, new_threads)

with open('bot.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("done")
