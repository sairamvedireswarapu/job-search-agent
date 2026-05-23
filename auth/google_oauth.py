"""Google OAuth flow + token validation + session management."""
import time
import secrets
import httpx
from google.oauth2 import id_token
from google.auth.transport import requests as grequests
from fastapi import HTTPException, Header, Cookie
from typing import Optional

from config import get_settings

settings = get_settings()

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# In-memory session store — fine for stateless Cloud Run + small user base.
# Keys: session_token → {user_id, email, name, exp}
# For 50+ users this stays fine — sessions are tiny and Cloud Run is sticky enough.
_sessions: dict[str, dict] = {}
SESSION_TTL = 60 * 60 * 24 * 7  # 7 days


def build_auth_url(state: str) -> str:
    """Return the Google OAuth consent URL."""
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{GOOGLE_AUTH_URL}?{query}"


async def exchange_code_for_tokens(code: str) -> dict:
    """Exchange OAuth code for id_token + access_token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": settings.OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        })
        resp.raise_for_status()
        return resp.json()


def verify_google_id_token(raw_id_token: str) -> dict:
    """Verify and decode a Google id_token. Returns claims dict."""
    try:
        claims = id_token.verify_oauth2_token(
            raw_id_token,
            grequests.Request(),
            settings.GOOGLE_OAUTH_CLIENT_ID,
        )
        return claims
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid Google token: {e}")


def create_session(user_id: str, email: str, name: str) -> str:
    """Create a session token, store it, return it."""
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "user_id": user_id,
        "email": email,
        "name": name,
        "exp": time.time() + SESSION_TTL,
    }
    return token


def get_current_user(
    authorization: Optional[str] = Header(default=None),
    session_token: Optional[str] = Cookie(default=None),
) -> dict:
    """
    FastAPI dependency. Accepts token as:
      - Authorization: Bearer <token>  (API / JS fetch)
      - session_token cookie           (browser)
    Returns the session dict with user_id, email, name.
    """
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
    elif session_token:
        token = session_token

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = _sessions.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="Session not found or expired")
    if time.time() > session["exp"]:
        _sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="Session expired")

    return session


def logout(
    authorization: Optional[str] = Header(default=None),
    session_token: Optional[str] = Cookie(default=None),
) -> None:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
    elif session_token:
        token = session_token
    if token:
        _sessions.pop(token, None)
