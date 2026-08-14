## 2023-08-14 - Background Threading for Telegram Bot Broadcaster
**Learning:** Telegram API broadcasters that iterate over many users and `sleep()` can severely block the main application thread.
**Action:** Move bulk processing/broadcasting tasks into daemon threads using `threading.Thread(target=..., daemon=True).start()` to avoid freezing up the main event loop.
