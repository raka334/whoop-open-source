# whoop-open-source

A unified WHOOP 5.0 data pipeline: pull **processed metrics from the official API** *and*
**raw sensor data straight off the strap over BLE**, and land both in one local SQLite store.

```
                  ┌───────────────────────┐
  Official API ──▶│                       │
  (OAuth2, v2)    │   unified Store       │◀── cycles / recoveries / sleeps / workouts
                  │   (SQLite)            │
  BLE strap    ──▶│                       │◀── hr_samples / raw_packets
  (fd4b protocol) │                       │
                  └───────────────────────┘
```

There is no cloud component. Everything is local and inspectable.

## Why two paths?

Neither source alone is complete:

| | Official API v2 | Reverse-engineered BLE |
|---|---|---|
| Data | Recovery, sleep, strain, workouts (WHOOP's *computed* scores) | Live HR + R-R, raw sensor packets, historical backlog |
| Raw? | No | **Yes** |
| Stability | Official, versioned | Unofficial, breaks on firmware updates |
| Access | Needs app approval | Needs the strap near the machine |

Use the API for reliable daily metrics and the BLE path for raw/real-time data.

## Layout

```
whoop/
├── config.py             env/.env configuration
├── store.py              unified SQLite schema + upserts (the shared seam)
├── api/                  official Developer API v2
│   ├── oauth.py          authorization-code flow + refresh
│   ├── client.py         authenticated GET + pagination
│   └── sync.py           pull collections into the store
├── ble/                  reverse-engineered strap protocol
│   ├── protocol.py       framing, CRCs, commands, HR parsing
│   └── stream.py         scan / HR stream / proprietary probe (bleak)
└── cli.py                command-line entry point
```

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[ble,dev]'     # drop [ble] if you only want the API side
cp .env.example .env            # fill in API credentials
```

The API half has **zero runtime dependencies** (stdlib `urllib`). `bleak` is only needed
for BLE commands.

## Usage

### Official API (v2)

Register an app at <https://developer-dashboard.whoop.com>, put the client id/secret in
`.env`, then:

```bash
whoop auth-url                 # open the printed URL, approve, copy the `code`
whoop auth-exchange '<code>'   # stores access + refresh tokens in the DB
whoop sync                     # pulls cycles, recoveries, sleeps, workouts
whoop sync --start 2026-01-01T00:00:00Z --resources recovery,sleeps
```

Tokens auto-refresh on expiry. Collection endpoints paginate automatically.

### BLE (raw path)

```bash
whoop ble-scan                 # find the strap, copy the address into .env
whoop ble-hr --seconds 120     # live HR + R-R intervals -> hr_samples
whoop ble-probe --seconds 30   # dump proprietary fd4b packets -> raw_packets
whoop stats                    # row counts for everything
```

### Protocol debugging

```bash
# decode a captured frame
whoop frames "aa100057 230423aa8ed469a96d0000005130fef3"
# {"name": "command:HELLO", "valid_crc": true, ...}

# build one to compare against a capture
whoop frames --build 0x23 4 0x23
```

## Protocol notes (WHOOP 5.0 / MG)

The proprietary protocol is partially documented in the community; these rules are
**verified against a real captured frame** in `tests/test_protocol.py`.

**Framing**

```
+----+--------+-------+---------------------------+----------+
| AA | LEN LE | CRC8  | payload                   | CRC32 LE |
+----+--------+-------+---------------------------+----------+
  1B    2B      1B      LEN - 4 bytes               4B
```

- `LEN = len(payload) + 4`, where `payload = packet_type | seq | cmd | data`.
- **CRC8** (poly `0x07`, init `0x00`) is computed over the **two LEN bytes only**.
- **CRC32** is `zlib.crc32(payload)`, little-endian.

**Packet types:** `0x23` command (app→strap), `0x24` response, `0x28` data, `0x30` event.
(The `0x72`/`0x73` in older write-ups are wrong.)

**GATT:** WHOOP 5.0/MG use service base `fd4b0001-…`; WHOOP 4.0 uses `61080001-…`.
Characteristics: `0002` write, `0003` response, `0004` event, `0005` data, `0007` memfault.

**Handshake:** HELLO (`0x23`) → clock sync (`0x0A`) → feature flags (`0x75/0x76/0x78`) →
config (`0x22`) → historical data (`0x16/0x17`). `RT_HR_ON` (`0x03`) starts live HR;
`0x0E` enables the standard `0x2A37` broadcast.

Standard BLE: HR `0x180D`/`0x2A37`, Battery `0x180F`/`0x2A19`, Device Info `0x180A`.
The HR service is **dormant** until the proprietary channel wakes it, and only emits after
the strap has been enrolled once in the official app.

### Platform gotcha

The `fd4b` channel requires a **bonded/encrypted** link. On macOS, Python/`bleak` gets
`Encryption is insufficient` after app-pairing, so `ble-probe` often returns zero packets
there. The proven-on-macOS path is **`ble-hr`** (standard `0x2A37`). For full proprietary
capture, run on Linux (BlueZ, which can bond) or use the iOS CoreBluetooth projects below.

## Prior art / credits

This project is an independent implementation of a community-documented protocol. It stands
on the shoulders of:

- [`ryanbr/noop`](https://github.com/ryanbr/noop) — active offline WHOOP companion (4.0/5.0).
- [`b-nnett/goose`](https://github.com/b-nnett/goose) — the original iOS (SwiftUI + Rust) project.
- [`elsayed85/whoop`](https://github.com/elsayed85/whoop) — Python/`bleak` research prototype.
- The `zulusierra.co` "Vestigator" write-up — sniffer methodology and the `0x23`/`0x24` correction.

Not affiliated with WHOOP, Inc. WHOOP® is a trademark of WHOOP, Inc.

## Legal

For personal use on hardware you own, and for interoperability research. WHOOP's Terms of
Service prohibit circumventing their service; the community justifies BLE-direct access
under data-portability (GDPR Art. 20) and interoperability (EU Directive 2009/24/EC Art. 6)
provisions. Do not use this to attack, clone, or commercially exploit WHOOP's systems.
Not medical advice.

## Roadmap

- [ ] Webhook receiver for API push updates
- [ ] Decode the 93-byte historical sensor record (SpO2, skin temp, motion)
- [ ] Interval-level API endpoints (`/cycle/{id}/recovery`, `/sleep/{id}`)
- [ ] CSV/Parquet export from the unified store
- [ ] Backfill BLE history (`0x16`/`0x17`) into the store
