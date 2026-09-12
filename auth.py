"""
auth.py — Authentication for Broadcast Hub
==========================================
First-boot setup flow, bcrypt password storage, and signed session cookies.

Flow:
  1. On first run, auth.json does not exist → every request is redirected
     to /setup where the user chooses a password.
  2. After setup, /login shows a styled HTML form.  On success a signed
     HttpOnly session cookie is issued.
  3. /logout clears the cookie.
  4. /settings/password lets a logged-in user change their password.

auth.json layout:
  {
    "password_hash": "<bcrypt hash>",
    "secret_key":    "<random hex — signs session cookies>"
  }

No plain-text passwords are ever written to disk.
"""

import os
import json
import html
import secrets
import logging
import tempfile
from pathlib import Path

import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

log = logging.getLogger("m2tsweb_fastapi.auth")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE     = Path(__file__).parent
AUTH_FILE = _HERE / "auth.json"

# Session cookie name and max-age (12 hours)
COOKIE_NAME    = "bh_session"
COOKIE_MAX_AGE = 12 * 60 * 60

# ---------------------------------------------------------------------------
# Auth state — loaded once at startup, updated on password change
# ---------------------------------------------------------------------------
_state: dict = {}   # {"password_hash": str, "secret_key": str}


def _load() -> dict:
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.error("Failed to read auth.json: %s", exc)
        return {}


def _save(data: dict) -> None:
    """Atomically write auth.json."""
    fd, tmp = tempfile.mkstemp(dir=_HERE, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, AUTH_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _serializer() -> URLSafeTimedSerializer:
    secret = _state.get("secret_key", "fallback-not-secure")
    return URLSafeTimedSerializer(secret, salt="bh-session")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init() -> None:
    """Load auth state from disk.  Call once at application startup."""
    global _state
    _state = _load()
    if _state:
        log.info("Auth: password is set — login required")
    else:
        log.warning("Auth: no password configured — setup required on first visit")


def is_configured() -> bool:
    """Return True if a password has been set (auth.json exists and is valid)."""
    return bool(_state.get("password_hash"))


def setup(password: str) -> None:
    """Hash *password* and write auth.json.  Called once from the setup route."""
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    secret_key    = secrets.token_hex(32)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    data = {"password_hash": password_hash, "secret_key": secret_key}
    _save(data)
    global _state
    _state = data
    log.info("Auth: password configured successfully")


def change_password(new_password: str) -> None:
    """Replace the stored password hash.  Invalidates all existing sessions."""
    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    new_hash   = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    # Rotate the secret key so old cookies are immediately invalidated
    new_secret = secrets.token_hex(32)
    data = {
        "password_hash": new_hash,
        "secret_key":    new_secret,
    }
    _save(data)
    global _state
    _state = data
    log.info("Auth: password changed — all sessions invalidated")


def verify_password(password: str) -> bool:
    stored = _state.get("password_hash", "")
    if not stored:
        return False
    return bcrypt.checkpw(password.encode(), stored.encode())


def make_session_cookie(response: Response) -> None:
    """Sign a session token and attach it as an HttpOnly cookie."""
    token = _serializer().dumps("authenticated")
    response.set_cookie(
        key      = COOKIE_NAME,
        value    = token,
        max_age  = COOKIE_MAX_AGE,
        httponly = True,
        samesite = "lax",
        secure   = False,   # Set to True if serving over HTTPS
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


def is_authenticated(request: Request) -> bool:
    """Return True if the request carries a valid, unexpired session cookie."""
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=COOKIE_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

def _base_html(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — Broadcast Hub</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap" rel="stylesheet">
<script>
  (function(){{
    try {{
      var t = localStorage.getItem('bh-theme');
      if (t && ['dark','mono','light'].includes(t))
        document.documentElement.setAttribute('data-theme', t);
    }} catch(e) {{}}
  }})();
</script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root, [data-theme="dark"] {{
    --bg:      #090d1a;
    --surface: #0b0f22;
    --border:  #1c2540;
    --text:    #c0cce8;
    --muted:   #3a4870;
    --accent:  #00e5ff;
    --accent-dim: rgba(0,229,255,.18);
    --accent-bdr: rgba(0,229,255,.4);
    --input-bg: #060810;
    --error-bg:  rgba(255,0,102,.1);
    --error-bdr: rgba(255,0,102,.3);
    --error-txt: #ff0066;
    --success-bg:  rgba(0,229,160,.08);
    --success-bdr: rgba(0,229,160,.2);
    --success-txt: #00e5a0;
    --label: #2a3560;
    --hint:  #2a3560;
    --back:  #2a3560;
  }}
  [data-theme="mono"] {{
    --bg:      #100e06;
    --surface: #0c0a04;
    --border:  #2a1e08;
    --text:    #e8d0a0;
    --muted:   #5a3818;
    --accent:  #ff6600;
    --accent-dim: rgba(255,102,0,.18);
    --accent-bdr: rgba(255,102,0,.45);
    --input-bg: #070604;
    --error-bg:  rgba(255,34,0,.1);
    --error-bdr: rgba(255,34,0,.3);
    --error-txt: #ff2200;
    --success-bg:  rgba(136,204,0,.08);
    --success-bdr: rgba(136,204,0,.2);
    --success-txt: #88cc00;
    --label: #3a2810;
    --hint:  #3a2810;
    --back:  #3a2810;
  }}
  [data-theme="light"] {{
    --bg:      #f3f5fa;
    --surface: #ffffff;
    --border:  #c8cedd;
    --text:    #1a1f38;
    --muted:   #6878a8;
    --accent:  #4d9fff;
    --accent-dim: rgba(77,159,255,.15);
    --accent-bdr: rgba(77,159,255,.45);
    --input-bg: #f8f9fd;
    --error-bg:  rgba(220,38,38,.07);
    --error-bdr: rgba(220,38,38,.25);
    --error-txt: #dc2626;
    --success-bg:  rgba(5,150,105,.07);
    --success-bdr: rgba(5,150,105,.2);
    --success-txt: #059669;
    --label: #b0b8d0;
    --hint:  #b0b8d0;
    --back:  #b0b8d0;
  }}

  body {{
    background: var(--bg); color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; padding: 36px 40px;
    width: 100%; max-width: 420px;
  }}
  .logo {{
    font-weight: 900; font-style: italic; font-size: 22px;
    text-transform: uppercase; margin-bottom: 28px; text-align: center;
    color: var(--text);
  }}
  .logo span {{ color: var(--accent); }}
  h2 {{
    font-size: 13px; font-weight: 900; text-transform: uppercase;
    letter-spacing: .12em; color: var(--muted); margin-bottom: 24px; text-align: center;
  }}
  .field {{ margin-bottom: 14px; }}
  label {{
    display: block; font-size: 10px; font-weight: 900;
    text-transform: uppercase; letter-spacing: .12em;
    color: var(--label); margin-bottom: 5px;
  }}
  input[type=password], input[type=text] {{
    width: 100%; background: var(--input-bg); border: 1px solid var(--border);
    color: var(--text); font-family: 'Inter', sans-serif;
    font-size: 14px; padding: 11px 13px; border-radius: 4px; outline: none;
  }}
  input:focus {{ border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-dim); }}
  .hint {{
    font-size: 11px; color: var(--hint); margin-top: 5px; line-height: 1.5;
  }}
  .btn {{
    width: 100%; margin-top: 20px; padding: 13px;
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 13px;
    text-transform: uppercase; letter-spacing: .08em;
    background: var(--accent); color: var(--bg);
    border: none; border-radius: 4px; cursor: pointer;
    transition: opacity .15s;
  }}
  .btn:hover {{ opacity: .88; }}
  .error {{
    background: var(--error-bg); border: 1px solid var(--error-bdr);
    color: var(--error-txt); font-size: 12px; padding: 10px 13px;
    border-radius: 4px; margin-bottom: 16px; line-height: 1.5;
  }}
  .success {{
    background: var(--success-bg); border: 1px solid var(--success-bdr);
    color: var(--success-txt); font-size: 12px; padding: 10px 13px;
    border-radius: 4px; margin-bottom: 16px; line-height: 1.5;
  }}
  .back {{
    display: block; text-align: center; margin-top: 18px;
    font-size: 11px; color: var(--back); text-decoration: none;
    text-transform: uppercase; letter-spacing: .1em;
  }}
  .back:hover {{ color: var(--muted); }}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Broadcast<span>Hub</span></div>
  {body}
</div>
</body>
</html>"""


def setup_page(error: str = "") -> HTMLResponse:
    err_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    body = f"""
    <h2>First-Time Setup</h2>
    {err_html}
    <form method="post" action="/setup">
      <div class="field">
        <label>Choose a password</label>
        <input type="password" name="password" autofocus autocomplete="new-password" required>
        <div class="hint">Minimum 8 characters. This protects the dashboard.</div>
      </div>
      <div class="field">
        <label>Confirm password</label>
        <input type="password" name="confirm" autocomplete="new-password" required>
      </div>
      <button class="btn" type="submit">Set Password &amp; Continue</button>
    </form>"""
    return HTMLResponse(_base_html("Setup", body))


def login_page(error: str = "", next_url: str = "/") -> HTMLResponse:
    err_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    safe_next = html.escape(next_url)
    body = f"""
    <h2>Sign In</h2>
    {err_html}
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{safe_next}">
      <div class="field">
        <label>Password</label>
        <input type="password" name="password" autofocus autocomplete="current-password" required>
      </div>
      <button class="btn" type="submit">Sign In</button>
    </form>"""
    return HTMLResponse(_base_html("Sign In", body))


def change_password_page(error: str = "", success: bool = False) -> HTMLResponse:
    msg_html = ""
    if success:
        msg_html = '<div class="success">Password changed. You have been signed out of all other sessions.</div>'
    elif error:
        msg_html = f'<div class="error">{html.escape(error)}</div>'
    body = f"""
    <h2>Change Password</h2>
    {msg_html}
    <form method="post" action="/settings/password">
      <div class="field">
        <label>Current password</label>
        <input type="password" name="current" autofocus autocomplete="current-password" required>
      </div>
      <div class="field">
        <label>New password</label>
        <input type="password" name="new_password" autocomplete="new-password" required>
        <div class="hint">Minimum 8 characters.</div>
      </div>
      <div class="field">
        <label>Confirm new password</label>
        <input type="password" name="confirm" autocomplete="new-password" required>
      </div>
      <button class="btn" type="submit">Change Password</button>
      <a href="/" class="back">← Back to dashboard</a>
    </form>"""
    return HTMLResponse(_base_html("Change Password", body))
