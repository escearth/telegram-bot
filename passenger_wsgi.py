"""cPanel/Passenger WSGI entry point for the Telegram Mini App.

Deployment (cPanel):
  1. Put the repo in your Python app's root (Setup Python App).
  2. Create the venv and `pip install -r requirements.txt`.
  3. Point the app at a subdomain (e.g. app.escearth.net) — cPanel
     issues HTTPS automatically (AutoSSL/Let's Encrypt).
  4. Passenger imports this file and serves the Mini App frontend + /api via
     `application`. The bot polling loop is started lazily on the first
     request (never at import time) so it also works when Passenger uses
     preload/smart-spawn and forks workers after importing this module.

Running `python3 bot.py` directly still works the same as before.
"""
import os
import threading
import logging

try:
    import fcntl
except ImportError:  # Windows / non-POSIX
    fcntl = None

import bot

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('passenger')

_BOT_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bot.lock')
_lock_file = None
_bot_started = False
# Guards in-process re-entry only. Cross-process exclusion comes from flock.
_start_lock = threading.Lock()


def _acquire_bot_process_lock():
    """Ensure only ONE process runs the Telegram polling loop.

    cPanel/Passenger may keep several processes alive for the same app.  If
    each one polled the same bot token, Telegram would kick them all off with
    a "Conflict: terminated by other getUpdates request".  The flock is
    released automatically when the holding process exits.
    """
    global _lock_file
    if fcntl is None:
        log.warning("fcntl unavailable: no process lock, multiple pollers possible")
        return True
    try:
        # O_NOFOLLOW: refuse a pre-created symlink at the lock path.
        fd = os.open(_BOT_LOCK_FILE,
                     os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        lock = os.fdopen(fd, 'r+')
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        _lock_file = lock  # keep the handle open for the process lifetime
        return True
    except OSError:
        return False


def _reset_after_fork():
    """A forked worker inherits the parent's memory but not its threads.

    Without this, a worker would believe the bot was already started (and would
    hold the parent's lock file description) while no polling thread exists in
    it.  The child drops the inherited handle so it can contend for the lock
    normally; only one worker ever wins it.
    """
    global _lock_file, _bot_started
    lock, _lock_file = _lock_file, None
    if lock is not None:
        try:
            lock.close()
        except OSError:
            pass
    _bot_started = False
    # The child also inherits the parent's single-instance flags. If the parent
    # (the polling owner) later dies, the child that wins the flock must be able
    # to start its own scheduler/polling loops instead of believing they are
    # already running in this process.
    bot._scheduler_started = False
    bot._webapp_started = False
    bot._polling_started = False


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _start_bot_once():
    """Start the bot once per process, and only in the process that wins the lock."""
    global _bot_started
    with _start_lock:
        if _bot_started:
            return False
        if not _acquire_bot_process_lock():
            log.info("Another process holds the bot lock; serving webapp only")
            _bot_started = True  # do not retry on every request
            return False
        try:
            threading.Thread(
                target=bot.start_bot, kwargs={'start_web': False},
                daemon=True, name='BotMain').start()
        except Exception:
            log.exception("Failed to start bot thread")
            return False
        _bot_started = True
        log.info("Bot thread started (pid %s)", os.getpid())
        return True


def _wsgi_app(environ, start_response):
    _start_bot_once()
    return bot._webapp_wsgi(environ, start_response)


application = _wsgi_app
