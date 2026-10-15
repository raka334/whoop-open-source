# whoop-open-source

A unified WHOOP data pipeline (4.0 and 5.0/MG): pull **processed metrics from the
official API** *and* **raw sensor data straight off the strap over BLE**, and land
both in one local SQLite store.

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
│   ├── protocol.py       family-aware framing (WHOOP 4 + 5/MG), CRCs, commands, safety
│   ├── sensors.py        WHOOP 5/MG decoders (packet 40 live HR, R21 IMU burst)
│   └── stream.py         scan / HR stream / probe / full raw capture (bleak)
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
whoop ble-hr --seconds 120     # live HR + R-R intervals -> hr_samples (works on macOS)
whoop ble-probe --seconds 30   # vendor handshake + dump those channels -> raw_packets
whoop capture --seconds 120 --imu   # full GATT enum + every notify char -> raw_packets
whoop stats                    # row counts for everything
whoop retention                # what the strap keeps on-device, and the GPS caveat
```

`capture` enumerates the whole GATT tree, subscribes to **every** notifiable
characteristic, and stores each frame with its `char_uuid` and detected family —
nothing is decoded away. `--imu` also arms the 100 Hz raw-motion producer.

### Export everything

```bash
whoop export                              # every table as NDJSON to stdout
whoop export --format csv --dest ./dump   # one CSV per table on disk
whoop export --table raw_packets           # just the raw frames
```

### Protocol debugging

```bash
# decode a captured frame (family auto-detected)
whoop frames "aa100057230423aa8ed469a96d0000005130fef3"
# {"family": "whoop4", "name": "command:HELLO_HARVARD", ...}

# rebuild the real WHOOP 5 connect hello, byte-for-byte
whoop frames --build 35 1 145 --family whoop5 --body 01
# aa0108000001e67123019101363e5c8d
```

## Protocol notes (WHOOP 4 **and** WHOOP 5/MG)

WHOOP uses **two different on-the-wire envelopes**. They share a command-number
vocabulary and a CRC32 trailer, but the headers are incompatible — a WHOOP 5
frame is *not* a WHOOP 4 frame with shifted offsets. Select the family before
parsing (`whoop/ble/protocol.py`).

**WHOOP 4 ("Harvard")** — service `61080001-…`

```
+----+--------+-------+--------------------------+----------+
| AA | LEN LE | CRC8  | type|seq|cmd|data        | CRC32 LE |
+----+--------+-------+--------------------------+----------+
  1B    2B      1B      LEN - 4 bytes                4B
```

- `LEN = len(inner) + 4`, `inner = type|seq|cmd|data`.
- **CRC8** (poly `0x07`, init `0x00`) over the **two LEN bytes only**.
- **CRC32** is `zlib.crc32(inner)`, little-endian.

**WHOOP 5 / MG ("puffin")** — service `fd4b0001-…`

```
+----+----+--------+-------+--------+-----------------+----------+
| AA | 01 | LEN LE | FIELD | CRC16  | inner (4-pad)   | CRC32 LE |
+----+----+--------+-------+--------+-----------------+----------+
  1B   1B    2B       2B      2B      LEN - 4 bytes       4B
```

- `FIELD` = `00 01` requests / `01 00` responses.
- `LEN = len(padded inner) + 4`; **total frame = LEN + 8**.
- **CRC16-Modbus** (reflected poly `0xA001`, init `0xFFFF`) over bytes `[0, 6)`.
- `inner = type|seq|cmd|body`, zero-padded to a multiple of 4.
- **CRC32** over the padded inner, little-endian.

Packet types: `0x23` command (app→strap), `0x24` response, `0x28` data / live HR,
`0x2B` realtime, `0x2F` historical, `0x30` event.

**GATT channels:** WHOOP 5/MG `fd4b0002` write, `0003` response, `0004` event,
`0005` data, `0007` memfault; WHOOP 4 uses `61080002…0005`.

**Handshake:** WHOOP 5 uses the fixed `CLIENT_HELLO`
(`aa 01 08 00 00 01 e6 71 23 01 91 01 36 3e 5c 8d`, cmd `0x91`); WHOOP 4 uses
`HELLO_HARVARD` (`0x23`). Then data range (`0x22`), config (`0x78`), historical
(`0x16`/`0x17`). `RT_HR_ON` (`0x03`) starts live HR; `0x0E` wakes the standard
`0x2A37` broadcast. Raw 100 Hz motion = `START_RAW_DATA` (`0x51`) then
`TOGGLE_IMU_MODE` (`0x6A`, payload `[0x01, 0x01]`); stop = `0x52` then `0x6A`
`[0x01, 0x00]`.

**Write safety:** `assert_writable()` allows known-benign opcodes, gates known
side-effect opcodes (e.g. `0x42` is `SET_ALARM_TIME` on 5.0 — it makes the strap
buzz, it is *not* `SET_CLOCK`), and **refuses unknown opcodes** by default. We do
not invent numbers for destructive operations (firmware load, force-trim, ship
mode, fuel-gauge reset); anything not listed is blocked.

Standard BLE: HR `0x180D`/`0x2A37`, Battery `0x180F`/`0x2A19`, Device Info `0x180A`.
The HR service is **dormant** until the vendor channel wakes it, and only emits
after the strap has been enrolled once in the official app.

### Data retention & the GPS myth

- The strap keeps **up to ~14 days** of data on-device and resumes syncing when
  your phone is reachable; a full 14-day catch-up can take up to ~1 hour.
  (One community post claims 30 days — WHOOP's own support docs say 14.)
- The durable store is the **type-47 historical record**, served from a page-slot
  ring buffer whose bounds come back from `GET_DATA_RANGE` (`0x22`).
- **WHOOP 5/MG has no GPS.** There is no GNSS chip and no GPS track on the strap.
  The only location-adjacent data is what the official API derives for a workout
  (`distance_meter`, `altitude_gain_meter`, `altitude_change_meter`), usually from
  the paired phone. Any claim of "GPS from the band" is a misunderstanding.
- Even a full raw capture does **not** recover everything: SpO2 is not recoverable
  offline from the time-multiplexed PPG stream, skin temperature is inconsistent,
  and sleep stages / overnight HRV need enough R-R coverage. Honesty beats hype.

### Platform gotcha

The vendor channel requires a **bonded/encrypted** link. On macOS, Python/`bleak`
gets `Encryption is insufficient` after app-pairing, so `ble-probe`/`capture`
often return zero packets there. The proven-on-macOS path is **`ble-hr`**
(standard `0x2A37`). For full vendor capture, run on Linux (BlueZ, which can bond)
or use the iOS CoreBluetooth projects below.

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

- [x] Family-aware protocol split (WHOOP 4 + 5/MG) with real-frame anchors
- [x] Raw capture: full GATT enum + every notify char + `char_uuid` provenance
- [x] Command write-safety classification (unknown opcodes blocked)
- [x] NDJSON/CSV export of every table
- [ ] HCI-log ingest (iOS `.pklg` / Android `btsnoop`) -> frame store
- [ ] Ground-truth correlator against the official CSV export
- [ ] Decode the remaining 5/MG records (R18/R20/R22/R26) against a real capture
- [ ] Webhook receiver for API push updates
- [ ] Backfill BLE history (`0x16`/`0x17`) into the store
