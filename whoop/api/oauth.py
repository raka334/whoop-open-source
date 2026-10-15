"""OAuth2 (authorization code) flow for the WHOOP Developer API."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from ..config import Config

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"


def authorization_url(config: Config, state: str = "whoop-open-source") -> str:
    """URL the user opens in a browser to grant access."""
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": " ".join(config.scopes),
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"Token request failed ({exc.code}): {detail}") from exc


def exchange_code(config: Config, code: str) -> dict[str, Any]:
    """Trade an authorization code for access + refresh tokens."""
    return _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": config.redirect_uri,
        },
    )


def refresh(config: Config, refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token for a fresh access token."""
    payload = _post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "scope": "offline",
        },
    )
    # WHOOP does not always echo the refresh token; keep the caller's copy.
    payload.setdefault("refresh_token", refresh_token)
    return payload
