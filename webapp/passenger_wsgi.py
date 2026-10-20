"""
passenger_wsgi.py — WSGI entry point for cPanel / Phusion Passenger.

Design notes
------------
This file NEVER imports bot.py. Importing bot would run its module-level
startup (polling, schedulers) inside Passenger's worker — that must not
happen. Instead it hosts the Mini App by itself:

  * ``/api/*``    -> reverse-proxied over HTTP to ``WEBAPP_BACKEND_URL``
                     (the Python backend bot.py starts on WEBAPP_HOST /
                     WEBAPP_PORT, which on cPanel should be run separately,
                     e.g. as an always-on app or via cPanel startup script).
  * anything else -> static files served straight out of webapp/ (the
                     directory this file lives in).

Set ``WEBAPP_BACKEND_URL`` in the Passenger environment (e.g.
"set-env WEBAPP_BACKEND_URL http://127.0.0.1:8080" in the app's .env or
cPanel environment UI) to point at the backend.
"""

import mimetypes
import os
import posixpath
import sys
import urllib.error
import urllib.request

# This file lives inside webapp/; the repo root sits one level up.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_BACKEND_URL = os.environ.get("WEBAPP_BACKEND_URL", "http://127.0.0.1:8080").rstrip("/")

# Static assets living in the webapp/ directory. Anything outside this
# allow-list is rejected (path-traversal safe), except files listed below.
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}


def _allowed_static(name):
    """Return a filesystem path for *name* inside webapp/, or None if the
    request would escape the directory or the file type is not servable."""
    # Normalize: collapse "..", strip the leading slash, drop empty parts.
    norm = posixpath.normpath(name).lstrip("/\\")
    if norm.startswith("..") or ":" in norm or norm in ("", "."):
        return None
    rel = norm.replace("/", os.sep)
    real = os.path.realpath(os.path.join(_HERE, rel))
    # Both realpath() (link resolution) and commonpath() (prefix check) make
    # traversal impossible even with URL-encoded slashes.
    try:
        if os.path.commonpath([real, _HERE]) != _HERE:
            return None
    except ValueError:  # different drive on Windows
        return None
    if not os.path.isfile(real) or os.path.islink(rel):
        return None
    return real


def _serve_static(path_qs):
    """Return the bytes, status and content type for a static request."""
    path = path_qs.split("?", 1)[0].split("#", 1)[0]
    if path in ("/", "") or path.endswith(".html"):
        fname = "index.html"
    else:
        fname = path.lstrip("/")
    # Explicit handler list first; anything else under webapp/ falls back to
    # the allow-listed static resolver.
    real = _allowed_static(fname)
    if real is None:
        return None
    ext = os.path.splitext(real)[1].lower()
    ctype = _STATIC_TYPES.get(ext) or mimetypes.guess_type(real)[0]
    if not ctype or ext not in _STATIC_TYPES:
        return None
    with open(real, "rb") as fh:
        return fh.read(), 200, ctype


def _proxy_api(environ):
    """Forward /api/* to WEBAPP_BACKEND_URL, preserving method, headers and body."""
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/")
    qs = environ.get("QUERY_STRING", "")
    url = _BACKEND_URL + path + (("?" + qs) if qs else "")

    # Only forward a small, safe set of inbound headers.
    out_headers = {}
    if environ.get("CONTENT_TYPE"):
        out_headers["Content-Type"] = environ["CONTENT_TYPE"]
    init = environ.get("HTTP_X_TELEGRAM_INIT_DATA")
    if init:
        # Strip CR/LF so the value cannot inject extra headers upstream.
        out_headers["X-Telegram-Init-Data"] = init.replace("\r", "").replace("\n", "")

    body = None
    length = int(environ.get("CONTENT_LENGTH") or 0)
    if method not in ("GET", "HEAD") and length > 0:
        body = environ["wsgi.input"].read(length)

    req = urllib.request.Request(url, data=body, method=method, headers=out_headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
            ct = resp.headers.get("Content-Type", "application/json; charset=utf-8")
            return content, resp.status, ct
    except urllib.error.HTTPError as e:  # pass statuses through verbatim
        try:
            content = e.read()
        except Exception:
            content = b'{"ok":false,"error":"upstream"}'
        ct = e.headers.get("Content-Type", "application/json; charset=utf-8") if e.headers else "application/json; charset=utf-8"
        return content, e.code, ct
    except Exception:
        return b'{"ok":false,"error":"backend unavailable"}', 502, "application/json; charset=utf-8"


def _response(start_response, status, ctype, body):
    if isinstance(body, str):
        body = body.encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", ctype),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ],
    )
    return [body]


def application(environ, start_response):
    """Passenger calls this per request. Errors never leak stack traces."""
    try:
        path = environ.get("PATH_INFO", "/") or "/"
        if path.startswith("/api/") or path == "/api":
            content, status, ctype = _proxy_api(environ)
            return _response(start_response, str(status), ctype, content)

        static = _serve_static(path)
        if static is None:
            return _response(
                start_response, "404 Not Found", "application/json; charset=utf-8",
                '{"ok":false,"error":"not found"}',
            )
        body, status, ctype = static
        return _response(start_response, str(status), ctype, body)
    except Exception:
        return _response(
            start_response, "502 proxy error", "application/json; charset=utf-8",
            '{"ok":false,"error":"wsgi"}',
        )


if __name__ == "__main__":
    # Local dev runner: serves this WSGI app standalone (statics + /api proxy)
    # without ever importing bot.py.
    from wsgiref.simple_server import make_server

    host = os.environ.get("WEBAPP_HOST", "127.0.0.1")
    port = int(os.environ.get("WEBAPP_PORT", "8080"))
    print(f"passenger_wsgi dev server on http://{host}:{port}")
    make_server(host, port, application).serve_forever()
