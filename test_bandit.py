import hmac, hashlib
import os
TELEGRAM_BOT_TOKEN = "test"
secret = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode('utf-8'), hashlib.sha256).digest()
secret2 = hmac.new(os.getenv("WEBAPP_SECRET", "WebAppData").encode('utf-8'), TELEGRAM_BOT_TOKEN.encode('utf-8'), hashlib.sha256).digest()
