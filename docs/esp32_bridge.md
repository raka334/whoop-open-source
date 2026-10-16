# ESP32-S3 WHOOP Bridge — Pairing Recipe & Protocol Session (2026-10-16)

The wall that BlueZ could not cross, crossed by an ESP32-S3 + NimBLE.
Firmware: `~/esp/whoop_central/` (esp-idf v5.3.1, NimBLE central).

## THE PAIRING RECIPE (100% repeatable)

```
ble_hs_cfg.sm_io_cap        = BLE_HS_IO_NO_INPUT_OUTPUT  (3)
ble_hs_cfg.sm_bonding       = 0        ← NO bonding bits in authreq
ble_hs_cfg.sm_mitm          = 0        ← no MITM
ble_hs_cfg.sm_sc            = 0        ← force LEGACY pairing
ble_hs_cfg.sm_our_key_dist   = 0x08 (LINK)
ble_hs_cfg.sm_their_key_dist = 0x0a (ID|LINK)
+ ble_store_config_init()              ← without this: rc=8 ENOTSUP silently
+ connect → PARING_COMPLETE status=0 → ENC_CHANGE status=0 (encrypted)
```

Strap behavior: accepts legacy JustWorks from ANY central with this profile;
session-only encryption (no bond stored on either side); link stable for
minutes; strap relaxes to itvl=204 (power-save, treats peer as the app).

BlueZ 5.66 could not produce this request (policy layer), which is why the
Realtek dongle path failed all night.

## Session transcript (esp32_session11.log)

Commands via fd4b0002 (handle 0x099b), responses on fd4b0003 (0x099d).
Frames built with the repo's own `whoop/ble/protocol.py::build_frame`
(family=whoop5) — byte-identical validation against CLIENT_HELLO.

| cmd | name | result |
|---|---|---|
| 0x91 | HELLO | 2×124B: serial `5B015354TX`, token `1b06635 5c01a07e1e924bbcf1d9…`, flags `ff 02` (likely auth/session challenge) |
| 0x03 | RT_HR_ON | ack `01` |
| 0x0E | STD_HR_BROADCAST | ack `02` |
| 0x1A | GET_BATTERY | **`0x4c` = 76%** |
| 0x22 | GET_DATA_RANGE | 84B: stored-data ranges with **unix timestamps** (0x6ad14e09 etc.) and durations (0x2e14 ≈ 3.3h) |
| 0x51 | START_RAW_DATA | ack `00 02`, no stream yet — needs IMU toggle (0x6A) and/or HELLO token response |

fd4b channel handles (live): command 0x099b, response 0x099d, event 0x09a0,
data 0x09a3, memfault 0x09a6. CCCD writes all succeed post-encryption.

## Next steps to full sensor data

**→ ACHIEVED (session13, same day).** The author-exact batch from
`stream.py::_write_batch` — critically `START_RAW_DATA` **with body `01`** followed
by `TOGGLE_IMU_MODE(enabled=True)` — opened the firehose:

- **879 notifications / ~170 s** on fd4b0005 (data channel)
- One **1244-byte R21 frame per second** (5×244 B fragments + 24 B tail):
  100 Hz six-axis IMU + third 100-sample channel, live epoch timestamps,
  CRC32 trailers — decodable by `whoop/ble/sensors.py::decode_imu_r21`
- Session log: `~/whoop-re/esp32_session13.log`

Remaining engineering (straightforward):
1. Reassemble fragments in ESP32 (or host-side) → feed `decode_imu_r21`
2. Serial bridge → `data/whoop_ble.sqlite` (hr_samples + raw_packets)
3. History offload (`SEND_HISTORICAL 0x16` with GET_DATA_RANGE catalog)
4. HELLO `ff 02` token challenge (optional — streams work without it)

## Artifacts

- Session logs: `~/whoop-re/esp32_session*.log` (9-11 = post-breakthrough)
- Firmware: `~/esp/whoop_central/` (v6: full command pipeline)
- Strap identity: WHOOP 5.0 `5B015354TX`, HW WG50_r45, FW 50.35.3.0,
  addr FD:17:43:A4:4D:82 (static random)

## History drain investigation (final, 2026-10-16)

Protocol fully cracked (from b-nnett/goose, verified empirically):

- `GET_DATA_RANGE (0x22)` empty body → catalog
- `SEND_HISTORICAL_DATA (0x16)` empty body → strap streams
- Metadata frames `0x38` (puffinMetadata): kind u16 LE @ frame[10:12]
  (1=Start, 2=End, 3=Complete)
- ACK: `HISTORICAL_DATA_RESULT (0x17)` payload = `[0x01] + historyEnd_frame[21:29]`
- Historical sequences conventionally start at 57

**Result on an app-enrolled strap:** 0x16 returns only the recent ~30-second
buffer (30 × R18 records, duplicated per request — 407 captured, all same
window). Manual default ACK does not advance the cursor; no 0x38 metadata
frames ever arrive; ack body `01 0b 00 00` likely = "no sync session".
Full-vault transfer appears gated to the strap's **enrolled owner**
(goose/noop users factory-reset and enroll to their client — same canned
HELLO, but they ARE the owner).

**Next lead:** `StrapUnlockApi` (`firmware-service/v4/strap/unlock-token`)
— the app fetches a cloud-issued unlock token and presents it to the strap.
We hold a valid cloud session; minting and presenting that token is the
candidate key to an owner-grade sync session without disturbing enrollment.

Safety preserved: no commit-ACKs were left enabled against the user's data;
the official app's sync path was never touched (coexistence mode active).
