"""Thin authenticated client for the WHOOP v2 REST API."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Iterator

from ..config import Config
from ..store import Store
from . import oauth

BASE_URL = "https://api.prod.whoop.com/developer/v2"
PROVIDER = "whoop"


class ApiError(RuntimeError):
    pass


class WhoopClient:
    """Handles token refresh and authenticated GETs."""

    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store

    # ------------------------------- auth --------------------------------
    def access_token(self) -> str:
        tokens = self.store.load_tokens(PROVIDER)
        if not tokens or not tokens.get("access_token"):
            raise SystemExit(
                "Not authenticated. Run `whoop auth-url`, open the URL, then "
                "`whoop auth-exchange '<code>'`."
            )
        if tokens.get("_expired"):
            refreshed = oauth.refresh(self.config, tokens["refresh_token"])
            self.store.save_tokens(PROVIDER, refreshed)
            tokens = self.store.load_tokens(PROVIDER)
        return tokens["access_token"]

    # ------------------------------- GET ---------------------------------
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{BASE_URL}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(clean)}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise ApiError(f"GET {path} failed ({exc.code}): {detail}") from exc

    def paginate(
        self,
        path: str,
        *,
        start: str | None = None,
        end: str | None = None,
        page_size: int = 25,
        max_pages: int = 200,
    ) -> Iterator[dict[str, Any]]:
        """Yield records across all pages of a collection endpoint."""
        cursor: str | None = None
        for _ in range(max_pages):
            params = {"limit": page_size, "start": start, "end": end, "nextToken": cursor}
            page = self.get(path, params)
            yield from page.get("records", [])
            cursor = page.get("next_token")
            if not cursor:
                return

    # ------------------------------ profile ------------------------------
    def profile(self) -> dict[str, Any]:
        return self.get("/user/profile/basic")

    def body_measurement(self) -> dict[str, Any]:
        return self.get("/user/measurement/body")
