# WHOOP 5.0 End-to-End: Full Reverse-Engineering Write-Up

*Session date: 2026-10-15/16 · Hardware: WHOOP 5.0 (PUFFIN, serial 5B015354TX) ·
Tools: REA MCP, jadx 1.5.2, Ghidra 12.1.4, Android emulator, Frida 16.5.9,
ESP32-S3 (NimBLE), BlueZ 5.66 + Realtek RTL8761 · All work on owner's own
account, hardware, and data.*

---

## TL;DR

Starting from "can we get all our data out?", one session produced:

1. **E2E export mystery solved** — WHOOP's "end-to-end encrypted" full-data
   export is encrypted **server-side**; the Android app contains no decryption
   code and holds no key. The export arrives by email; only the recipient can
   open it.
2. **Firmware acquisition** — every firmware image WHOOP's API will serve for
   the account's devices, including the band's NORDIC image (unencrypted,
   Ghidra-analyzable) and the battery-pack AMBIQ image.
3. **A reusable cloud-auth pipeline** — Cognito login chain reversed, session
   token extracted from the official app running in a rooted emulator via a
   Frida `Cipher.doFinal` hook, bypassing Cloudflare's TLS fingerprinting.
4. **A complete BLE security audit** — pairing-policy matrix from six HCI
   captures, CVE applicability analysis (none apply), BLESA-class test blocked
   only by hardware (no CVE-grade weakness found: the strap is well-defended).
5. **The breakthrough** — an **ESP32-S3 running NimBLE pairs with the strap on
   demand** using a legacy-JustWorks profile BlueZ cannot produce, opens an
   encrypted session, speaks the vendor protocol, and **streams 100 Hz raw
   six-axis IMU data** on command.
6. **Database integration** — a serial→SQLite bridge (this repo,
   `tools/esp32_bridge.py`) reassembles fragmented frames, decodes them with
   the repo's own modules, and stores HR + raw packets in the existing
   `whoop_ble.sqlite` schema. Validation: decoded accelerometer shows the
   gravity vector (~0.98 g) at rest.

The five detailed ledgers live in `docs/`: `e2e_export_re.md`,
`firmware_fetch.md`, `ble_live_linux.md`, `esp32_bridge.md`, and this file.

---

## 1. The E2E export: where the key lives (it doesn't)

Question: can the app decrypt the "export all data" archive locally?

**Method.** APK pulled from the owner's phone with REA `pull_adb_package`
(`com.whoop.android` v5.472.0, base sha256 `c9b50724…`). REA `inspect_artifact`
inventoried 4,952 entries (Evidence `ev_b8889b93…`); jadx decompiled the
relevant dex files.

**Findings (all static-observed):**
- Export module `com.whoop.memberdataexport`: `POST
  /member-data-export-service/v1/member-data-export-details` with body
  `{email}`; response carries only UI copy + `recipient_email_display`.
- Download path (`DownloadAction` → `FileExportUtil`) is a plain byte copy via
  Android SAF. No `Cipher`, no KDF, no passphrase string anywhere in 11 dex
  files; the only AES constants belong to the Iterable analytics SDK; the only
  unzip code is the firmware updater.
- No custom crypto `.so` in the arm64 split.

**Conclusion:** the archive is produced and keyed server-side; the app is a
request/download UI. "E2E" = only the member's email grants access. A local
decryptor is therefore impossible by design — the file plus its emailed
credential is the complete artifact. *(Still pending: operator requesting the
export so the container format can be recorded.)*

## 2. Firmware: everything the cloud will hand over

`OtaFirmwareUpdateApi` (jadx) revealed `firmware-service/v4/firmware/{check,
version,result}`. With a live session token (below) and the required headers
(`X-WHOOP-Device-Platform/OS/Model`):

- Error messages leak the full device enum: `COPLEY, HARVARD, PUFFIN,
  MAVERICK, GOOSE, MONUMENT, SYMPHONY, MERLIN, SWAN, TEST`, the chip enum
  `[MAXIM, NORDIC, PEARL, RUGGLES, AMBIQ]`, and the account's UserId.
- PUFFIN serves exactly one image (server: *"Expected: [NORDIC]"*): ZIP
  delivered **base64-inline**; member `usb_update_puffin_3.30.5.0_…bin`
  (410,019 B, sha256 `5230866c…`).
- MAVERICK/GOOSE (5.0 battery pack): `maverick-50.42.1.0.zbin` (1,013,584 B).
- The NORDIC image is a raw, **unencrypted** nRF52 image: magic `3db8f396`,
  RAM-init TLV footer (module table with flash→RAM copy ranges), vector table
  at offset `0x200` (SP `0x2001c500`). Strings reveal nRF Connect SDK 3.5.99,
  SoftDevice Controller, MCUboot, Memfault, and the strap's own protocol code
  (`whoop_interface_incoming_packet_verify` with "Header CRC ERR" /
  "Wrong Packet Size" checks — independent confirmation of the repo's framing).
- Ghidra 12.1.4 headless analysis succeeded (JDK 21 + `ARM:LE:32:v8`, vector
  table seeded); note the image is boot-relocated (zero absolute string
  pointers), so static xref work requires loader emulation — future work.

All images + digests: `~/Desktop/whoop-firmware/` and ledger
`docs/firmware_fetch.md`.

## 3. Cloud auth chain (and the token pipeline)

Reversed from the APK: login is **AWS Cognito** proxied at
`api.prod.whoop.com/auth-service/v3/whoop` (client id
`5311fm7hqak9lrnpht6kp2p5uh`), exchanged at `auth-service/v2/whoop/refresh`
(`Bearer <cognito access token>` → WHOOP tokens). Plain `curl` is blocked by
Cloudflare TLS fingerprinting (403); `curl_cffi` with
`impersonate="chrome124"` passes. Password login failed for the credentials
tried, and app tokens are Keystore-encrypted at rest
(`encryptedAccessToken` in DataStore).

**Token extraction pipeline (no root phone needed):**
1. Official Android emulator (API 34 google_apis x86_64, `adb root`).
2. Genuine split APKs installed (`install-multiple`; ARM translation fine).
3. Owner logs in normally in the emulator UI.
4. frida-server 16.5.9 (x86_64) + spawn-time hook on
   `javax.crypto.Cipher.doFinal` → decrypted access JWT captured
   (iss `cognito-idp.us-west-2.amazonaws.com/us-west-2_rYv1jh`, RS256).
5. That token authenticates `firmware-service` directly.

Scripts: `~/whoop-re/{whoop_fw_fetch.py,fw_with_token.py,frida_spawn_dump*.py}`.
Secrets stay in `~/whoop-re` (mode 600), never in the repo. Lessons recorded:
frida 17 dropped the bundled Java bridge (use 16.x); frida-server and client
versions must match; the WHOOP process name is `WHOOP`.

## 4. BLE security audit (pre-breakthrough)

Six HCI captures (`sudo btmon -w`, user-run one-liners) mapped the strap's
pairing policy precisely:

| Initiator request (us) | Strap response |
|---|---|
| full keydist / post-GATT state | instant `Pairing Failed` |
| any request with MITM flag | instant reject |
| legacy, minimal keydist (`0x08/0x0a`), no MITM | engages → User Confirmation (hint) |
| its own key distribution | **none** → successful pairing is session-only |

Unauthenticated access matrix: connections, HR (0x180d), battery, device-info
readable by anyone in range; every fd4b characteristic (incl. CCCD writes)
requires the encrypted link; Just-Works pairing from BlueZ is refused at the
policy layer (agent games) or by the strap. CVE audit against live NVD
records: KNOB/BIAS are BR/EDR-only; SweynTooth targets 2018-19 legacy
SoftDevices (this strap: 2025 SoftDevice Controller — and brick-risk to test);
no WHOOP-specific CVE exists; BLESA-class owner impersonation requires an
identity-spoofable controller, which Realtek+BlueZ 5.66 is not. Verdict:
**defended in depth; no shortcut found.** Full matrix: `docs/ble_live_linux.md`.

## 5. The breakthrough: ESP32-S3 pairs where BlueZ cannot

Idea: bypass the host stack entirely. An ESP32-S3 with NimBLE gives surgical
SMP control. The winning configuration (100% repeatable, dozens of sessions):

```c
ble_hs_cfg.sm_io_cap        = BLE_HS_IO_NO_INPUT_OUTPUT;
ble_hs_cfg.sm_bonding       = 0;   /* NO bonding bits — critical */
ble_hs_cfg.sm_mitm          = 0;
ble_hs_cfg.sm_sc            = 0;   /* force LEGACY pairing — critical */
ble_hs_cfg.sm_our_key_dist   = 0x08;             /* LINK */
ble_hs_cfg.sm_their_key_dist = 0x0a;             /* ID|LINK */
ble_store_config_init();    /* without this: silent rc=8 ENOTSUP */
```

Sequence: passive scan → connect (params 24-40/300) → `security_initiate` →
`PARING_COMPLETE status=0` → `ENC_CHANGE status=0` (**link encrypted**) →
service discovery → subscribe fd4b0003/4/5/7 (all CCCDs accepted) → command
batch. The strap settles into power-save (itvl 204) as if we were the app.

**Gotchas that cost hours (all recorded in `docs/esp32_bridge.md`):**
missing `ble_store_config_init()` (silent ENOTSUP); bonding bits must be
clear; keydist rotation only confuses; heartbeat task must not `return`
(FreeRTOS abort); UUID little-endian byte order; NRF log level knobs.

## 6. Speaking the protocol

Frames built with **this repo's own** `whoop/ble/protocol.py::build_frame`
(family `whoop5`), byte-identical to the canned `CLIENT_HELLO`. Commands and
observed results (session logs `esp32_session11-13.log`):

| cmd | name | strap response |
|---|---|---|
| 0x91 | HELLO | serial + session token (`ff 02` challenge unanswered — streams work anyway) |
| 0x1A | GET_BATTERY | `0x4c` = 76% |
| 0x22 | GET_DATA_RANGE | catalog of stored data: unix timestamps + durations |
| 0x03 / 0x0E | RT_HR_ON / STD_HR | acks |
| 0x51 body `01` | START_RAW_DATA | arms the producer (**body byte required**) |
| 0x6A body `01 00 00 00` | TOGGLE_IMU_MODE(enabled) | opens the firehose |

Result: **one 1,244-byte R21 frame per second** on fd4b0005 (5×244 B
fragments), each carrying 100 samples of six-axis IMU + a third channel,
live epoch timestamps, CRC32 trailer — the exact `decode_imu_r21()` format
this repo already implemented.

## 7. Bridge to the database

`tools/esp32_bridge.py` (host side):

- reads the firmware's `NOTIFY` lines from `/dev/ttyACM0`
- reassembles fragments by scanning for `aa 01` + declared length (`+8` total)
- dispatches on parsed packet type: `0x28` → `decode_live_hr` → `hr_samples`;
  `0x2B`/1244 B → `decode_imu_r21` → `raw_packets.decoded_json`; everything
  else stored raw
- tracks `capture_sessions`

Validation run: 11 R21 frames/60 s, accelerometer resting vector
**(0.979, −0.137, 0.126) g** — gravity, correctly signed and scaled. *(Known
polish items: R21 trailer CRC flag currently False under host reassembly —
data decodes correctly; gyro_count field reads 0 in this firmware variant.)*

## 8. Reproduction

1. **ESP32 firmware** — `~/esp/whoop_central/` (esp-idf v5.3.1; sdkconfig:
   BT + NimBLE central). `idf.py -p /dev/ttyACM0 flash`. Phone Bluetooth off.
   The board self-pairs within seconds of the strap advertising.
2. **Bridge** — from repo root:
   `~/whoop-re/.venv-fw/bin/python tools/esp32_bridge.py --seconds 300`
   (needs `pyserial`; everything else is stdlib + repo modules).
3. **Inspect** — `data/whoop_ble.sqlite` (`hr_samples`, `raw_packets`,
   `capture_sessions`); dashboard: `whoop web`.
4. **Cloud side** — token pipeline per §3 for firmware/other services.

## 9. Open items

- [ ] R21 host-side CRC recheck + gyro_count variant handling
- [ ] ESP32 binary framing over serial (drop the hex-text transport)
- [ ] History offload: `SEND_HISTORICAL (0x16)` with the GET_DATA_RANGE
      catalog → ~14 days of stored raw records
- [ ] HELLO `ff 02` session-token semantics
- [ ] E2E export container analysis (operator to request the export)
- [ ] Firmware deep static RE (relocated image; loader emulation)
- [ ] Port the ESP32 pairing recipe as a `whoop ble` backend

## 10. Legal & ethics

Everything above was performed on the author's own account, own hardware, and
own data, for interoperability research (GDPR Art. 20 rationale, consistent
with this repo's stance). No credentials or tokens are committed; session
secrets live outside the repo with mode 600. Not affiliated with WHOOP Inc.;
not medical advice. The strap's defensive design was verified, not defeated
by exploitation: the ESP32 path uses the standard pairing the strap itself
offers to non-bonded centrals.
