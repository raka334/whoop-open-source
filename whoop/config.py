"""Runtime configuration, loaded from environment (optionally via a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no interpolation, never overwrites."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...]
    api_db_path: Path
    ble_db_path: Path
    ble_address: str
    device_id: str

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        root = Path(__file__).resolve().parent.parent
        _load_dotenv(env_file or root / ".env")

        scopes = tuple(
            os.environ.get(
                "WHOOP_SCOPES",
                "read:recovery read:cycles read:workout read:sleep "
                "read:profile read:body_measurement",
            ).split()
        )

        def _resolve(var: str, default: str) -> Path:
            path = Path(os.environ.get(var, default))
            return path if path.is_absolute() else root / path

        # The two sources live in separate files; see whoop/store.py.
        api_db_path = _resolve("WHOOP_API_DB_PATH", "data/whoop_api.sqlite")
        ble_db_path = _resolve("WHOOP_BLE_DB_PATH", "data/whoop_ble.sqlite")

        return cls(
            client_id=os.environ.get("WHOOP_CLIENT_ID", ""),
            client_secret=os.environ.get("WHOOP_CLIENT_SECRET", ""),
            redirect_uri=os.environ.get(
                "WHOOP_REDIRECT_URI", "http://localhost:8721/callback"
            ),
            scopes=scopes,
            api_db_path=api_db_path,
            ble_db_path=ble_db_path,
            ble_address=os.environ.get("WHOOP_BLE_ADDRESS", ""),
            device_id=os.environ.get("WHOOP_DEVICE_ID", ""),
        )

    def require_api(self) -> None:
        if not self.client_id or not self.client_secret:
            raise SystemExit(
                "Missing WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET.\n"
                "Copy .env.example to .env and fill it in "
                "(register at https://developer-dashboard.whoop.com)."
            )
