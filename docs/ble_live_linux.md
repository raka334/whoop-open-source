# Live BLE on Linux — WHOOP 5.0 (2026-10-16 session)

First successful Linux GATT session against the enrolled WHOOP 5.0 strap,
from this Debian box (Realtek USB radio, BlueZ). Log: `~/whoop-re/ble_session3.log`.

## Device identity (Device Information service, unencrypted reads)

| Field | Value |
|---|---|
| Manufacturer | WHOOP Inc. |
| Model | 5.0 |
| Serial | 5B01535458 |
| Hardware | WG50_r45 |
| Firmware (composite) | 50.35.3.0 |
| Battery (0x2a19) | 77% (`0x4D`) |
| Address | FD:17:43:A4:4D:82 (random) |

Note: server-sanctioned NORDIC image is `3.30.5.0`; the 0x2a26 value
`50.35.3.0` is the composite/app-visible version (scheme: `50.` = puffin 5.0).

## GATT map (handles observed live)

| Service | Char | Props | Handle | Notes |
|---|---|---|---|---|
| 0x180d Heart Rate | 0x2a37 | notify | 33 | **streams HR @1 Hz + RR intervals, unencrypted** |
| 0x180f Battery | 0x2a19 | read,notify | 137 | readable unencrypted |
| 0x180a Device Info | 0x2a29/24/25/26/27 | read | 49–57 | readable unencrypted |
| 0x1801 GATT | 0x2a05 | indicate | 17 | |
| fd4b0001 vendor | fd4b0002 | write,write-nr | 2458 | command channel |
| | fd4b0003 | notify | 2460 | response |
| | fd4b0004 | notify | 2463 | event |
| | fd4b0005 | notify | 2466 | data (raw streams) |
| | fd4b0007 | notify | 2469 | memfault |

## Observed

- `pair` → strap **accepts the LE link but rejects Just-Works pairing**
  (`org.bluez.Error.AuthenticationFailed`). Bonds only with app-provisioned
  keys (enrollment-time). Same wall as macOS, now proven on BlueZ.
- HR + RR stream confirmed ~25 s: 83–86 bpm, RR 0.65–0.73 s (HRV-grade data),
  exactly the standard 0x180d format (flags byte + uint8/uint16 bpm + RR list,
  RR = value/1024 s).
- Connection is stable and fast (~0.6 s connect), service resolution ~3 s.

## What this unlocks vs. what remains

- ✅ Live HR/RR capture pipeline on Linux (feeds `hr_samples` schema directly)
- ❌ fd4b raw channels (100 Hz IMU, history offload R-records): gated on an
  authenticated bond. Options, cheapest first:
  1. Re-enrollment experiment: factory-reset strap → Linux bonds during the
     enrollment window (needs enrollment-protocol RE; strap daily-use breaks
     until re-enrolled via app)
  2. Extract LTK from the phone (needs phone root — not available)
  3. Deep static RE of the relocated firmware image (long game)

## Security audit addendum (2026-10-16, later session)

### Pairing policy (from 6 HCI captures, `pair*.snoop`)

| Initiator request | Strap response |
|---|---|
| Full keydist (bonded/GATT-resolved device state) | instant `Pairing Failed` |
| Any request with MITM flag | instant reject |
| Legacy, minimal keydist (0x08/0x0a), pre-connection | Pairing Response (Legacy, Bonding) → User Confirmation (hint) |
| Strap's own key distribution | **none** → successful pairing would be session-only, no stored bond |

After repeated attempts the strap rejects in ~50 ms (rate-limit / cache).

### CVE applicability (verified against NVD records)

- **No WHOOP-device CVE exists** (NVD keyword search; GitHub exploit repos: 0)
- CVE-2019-9506 (KNOB), CVE-2020-10135 (BIAS): BR/EDR-only — strap is BLE-only
- SweynTooth family (CVE-2019-17060 etc.): targets 2018-19 legacy S132/S140
  SoftDevices; strap runs nRF Connect SDK 3.5.99 + modern SoftDevice Controller
  (2025) — structurally immune; also brick-risk to test
- CVE-2023-45866 (BlueZ HID): wrong direction (host-as-victim)

### BLESA-class owner impersonation: BLOCKED by hardware/tooling

- Phone identity (via `adb shell settings get secure bluetooth_address`):
  `5C:A0:6C:2B:EB:AA`, BT name "idk"
- `btmgmt publicaddr` absent in bluez 5.66; Realtek RTL8761 cannot change
  identity regardless → spoof impossible on this adapter
- Verdict: needs a controller with settable identity (nRF52840 dongle)

### Remaining paths to raw sensor data

1. nRF52840 USB dongle + WHAD/btlejack: raw SMP control, identity spoofing,
   passive connection sniffing (~$40)
2. Re-enrollment experiment (factory reset → Linux bonds in enrollment window)
3. Deep static RE of relocated NCS firmware (auth/bond logic)

## Toolchain on this box

`~/whoop-re/.venv-fw` (bleak, curl_cffi, frida 16.5.9), BlueZ + bluetoothctl,
Ghidra 12.1.4 @ `~/tools/ghidra_12.1.4_PUBLIC` (JDK 21 Temurin @ `~/tools/jdk-21…`).
