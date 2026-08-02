"""cPanel/Passenger WSGI entry point for the Telegram Mini App.

Deployment (cPanel):
  1. Put the repo in your Python app's root (Setup Python App).
  2. Create the venv and `pip install -r requirements.txt`.
  3. Point the app at a subdomain (e.g. app.escearth.net) — cPanel
     issues HTTPS automatically (AutoSSL/Let's Encrypt).
  4. Passenger imports this file, starts the bot in the background,
     and serves the Mini App frontend + /api via `application`.

Running `python3 bot.py` directly still works the same as before.
"""
import os
import threading

try:
    import fcntl
except ImportError:  # Windows / non-POSIX
    fcntl = None

import bot

_BOT_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bot.lock')


def _acquire_bot_process_lock():
    """Ensure only ONE process runs the Telegram polling loop.

    cPanel/Passenger may keep several processes alive for the same app.  If
    each one polled the same bot token, Telegram would kick them all off with
    a "Conflict: terminated by other getUpdates request".  The flock is
    released automatically when the holding process exits.
    """
    if fcntl is None:
        return True
    try:
        lock = open(_BOT_LOCK_FILE, 'a+')
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        # Keep a reference so the lock file is not closed/GC'd while alive.
        bot._passenger_lock_file = lock
        return True
    except OSError:
        return False


if not getattr(bot, '_passenger_bot_started', False):
    bot._passenger_bot_started = True
    if _acquire_bot_process_lock():
        threading.Thread(
            target=bot.start_bot, kwargs={'start_web': False},
            daemon=True, name='BotMain').start()

application = bot._webapp_wsgi
