"""Cross-source coverage and reconciliation -- explicit, opt-in, read-only.

The two sources are separate databases (see :mod:`whoop.store`). Nothing joins
them unless you ask. This module is the *only* place that does, and it opens both
files **read-only** so a reconciliation run can never mutate either source.

The API and the BLE path are complementary, not duplicates: the API holds WHOOP's
computed daily metrics, the strap holds raw high-rate samples. Where their windows
overlap, the BLE stream should *corroborate* the API's averaged heart rate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .store import ApiStore, BleStore, _norm_ts


def _open(store_cls: type, path: Path | str, readonly: bool):
    p = Path(path)
    if readonly and p.exists():
        return store_cls(p, readonly=True)
    # A missing file is created empty so the caller sees zeros, not an error.
    return store_cls(p)


class Combined:
    """A read-only view over both source databases."""

    def __init__(self, api: ApiStore, ble: BleStore) -> None:
        self.api = api
        self.ble = ble

    @classmethod
    def open(
        cls,
        api_path: Path | str,
        ble_path: Path | str,
        *,
        readonly: bool = True,
    ) -> "Combined":
        return cls(_open(ApiStore, api_path, readonly), _open(BleStore, ble_path, readonly))

    def close(self) -> None:
        self.api.close()
        self.ble.close()

    def __enter__(self) -> "Combined":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------- coverage ----------------------------
    def coverage(self) -> dict[str, dict[str, Any]]:
        return {**self.api.coverage(), **self.ble.coverage()}

    # ---------------------------- reconciliation -------------------------
    def reconcile(
        self, *, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.api.cycles_with_hr():
            if start and (_norm_ts(r["end"]) or "") < _norm_ts(start):
                continue
            if end and (_norm_ts(r["start"]) or "") > _norm_ts(end):
                continue
            s = self.ble.hr_summary(r["start"], r["end"])
            api_avg = r.get("average_heart_rate")
            ble_avg = round(s["avg_bpm"], 1) if s.get("avg_bpm") is not None else None
            out.append(
                {
                    "kind": r["kind"],
                    "id": r["id"],
                    "start": r["start"],
                    "end": r["end"],
                    "api_avg": api_avg,
                    "api_max": r.get("max_heart_rate"),
                    "ble_n": s.get("n") or 0,
                    "ble_avg": ble_avg,
                    "ble_max": s.get("max_bpm"),
                    "delta": (
                        round(ble_avg - api_avg, 1)
                        if ble_avg is not None and api_avg is not None
                        else None
                    ),
                }
            )
        return out
