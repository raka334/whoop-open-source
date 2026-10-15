# Handoff: whoop-open-source

*Session handoff — read this before touching anything. Protocol details and
full command reference live in `README.md`; this file is about **state**:
what's done, what's blocked, what's next.*

## What this is

A two-source WHOOP 4/5 data pipeline, fully local, no cloud component:

```
Official API v2 ──▶ data/whoop_api.sqlite   (cycles, recoveries, sleeps, workouts)
BLE strap fd4b  ──▶ data/whoop_ble.sqlite   (hr_samples, raw_packets, capture_sessions)
                        │
                  whoop reconcile  (read-only join + HR cross-check)
```

The two databases are never mixed. Either can be deleted/rebuilt without the
other. Python 3, stdlib-only for the API side; `bleak` only for BLE.

## State at handoff time

- **Tests:** 51/51 pass (`source .venv/bin/activate && python -m pytest tests/ -q`)
- **Git:** `master`, clean tree, pushed to `github.com/raka334/whoop-open-source` (public)
- **Install:** `.venv` has `.[ble,dev]` installed; CLI `whoop` works
- **`.env`:** created from `.env.example` — but `WHOOP_CLIENT_ID`,
  `WHOOP_CLIENT_SECRET`, and `WHOOP_BLE_ADDRESS` are **still empty** (the
  actual blockers; see Next steps)
- **`data/`:** does not exist yet — nothing has ever synced
- **Roadmap through "web dashboard":** all done ✅

## Blockers, in order

1. **API credentials.** Register an app at
   <https://developer-dashboard.whoop.com>, put id/secret in `.env`, then:
   `whoop api auth-url` → approve in browser → `whoop api auth-exchange '<code>'`
   → `whoop api sync`. Approval of a personal dev app can take a few days.
2. **Strap enrollment.** The HR service only broadcasts after the strap has
   been enrolled once in the official WHOOP app. No enrollment → no BLE data.
3. **macOS cannot bond.** This machine is a Mac. CoreBluetooth won't give
   Python/bleak an encrypted link, so `ble probe` / `ble capture` fail with
   `Encryption is insufficient`. **Only `ble hr` works on macOS.** Full vendor
   capture (raw packets, 100 Hz IMU, on-device history) needs Linux/BlueZ
   (which can bond) or the iOS CoreBluetooth prior-art projects.

## Next steps (ordered)

1. Fill `.env` API credentials → `whoop api auth-url` / `auth-exchange` / `sync`.
2. `whoop ble scan` → copy strap address into `.env` as `WHOOP_BLE_ADDRESS`
   → `whoop ble hr --seconds 120` (proven on macOS).
3. For the full raw unlock: run `whoop ble capture --seconds 120 --imu` on a
   Linux box with BlueZ, bonded to the strap.
4. `whoop sync-all` for both sources + reconciliation; `whoop web` for the
   dashboard at `http://127.0.0.1:8765/`.

## Roadmap (not yet built)

- BLE history offload loop (durable-commit-then-ACK, replay-safe)
- HCI-log ingest (iOS `.pklg` / Android `btsnoop`) → frame store
- Ground-truth correlator against official CSV export
- Decode remaining 5/MG records (R18/R20/R22/R26) — needs a real Linux capture
- Webhook receiver for API push updates

## Gotchas that will bite you

- **Write safety:** `whoop/ble/protocol.py::assert_writable()` blocks unknown
  opcodes on purpose. `0x42` is `SET_ALARM_TIME` on 5.0 (strap buzzes) — it is
  **not** SET_CLOCK. Don't "fix" that mapping.
- **Two envelopes:** WHOOP 4 ("Harvard") and WHOOP 5/MG ("puffin") frames are
  incompatible; family must be selected before parsing.
- **HR dedup:** BLE samples dedupe on `(device, timestamp, bpm)` — re-running
  `sync-all` never double-counts.
- **Reconcile is read-only** and opens both DBs read-only. Keep it that way.
- **Reality check:** Whoop 5/MG has **no GPS**. SpO2/skin-temp are not
  recoverable offline from raw PPG. On-device history ≈ 14 days. Don't promise
  more than the strap holds.

## File map

| File | Role |
|---|---|
| `whoop/config.py` | env/.env config, both DB paths |
| `whoop/store.py` | `ApiStore` + `BleStore` (separate schemas) |
| `whoop/api/{oauth,client,sync}.py` | OAuth2 flow, GET+pagination, collections |
| `whoop/ble/protocol.py` | family-aware framing (4 vs 5/MG), CRCs, write safety |
| `whoop/ble/sensors.py` | WHOOP 5/MG decoders (packet 40 HR, R21 IMU) |
| `whoop/ble/stream.py` | scan / hr / probe / capture via bleak |
| `whoop/sync.py` | concurrent dual-source sync |
| `whoop/reconcile.py` | read-only cross-source join |
| `whoop/web/` | stdlib dashboard, one panel per source |
| `whoop/cli.py` | every command |
| `tests/` | 51 tests, no hardware needed |

## Legal

Personal use on hardware you own, interoperability research. WHOOP's ToS
prohibits circumvention; the community rationale is GDPR Art. 20 data
portability. Not affiliated with WHOOP, Inc. Not medical advice.
