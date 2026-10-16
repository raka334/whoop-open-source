# WHOOP 5.0 Firmware Fetch — RE Ledger (2026-10-16 session)

Goal: download the WHOOP 5.0 (puffin) strap firmware image for static analysis,
using only the user's own account, app, and hardware. **Result: captured.**

## Artifacts

| Artifact | Path | Identity |
|---|---|---|
| WHOOP app (base) | `~/whoop-re/com.whoop.android/base.apk` | sha256 `c9b50724cad2e7d1…` (v5.472.0) |
| arm64 split | `~/whoop-re/com.whoop.android/split_config.arm64_v8a.apk` | sha256 `978a8cbd2f8c4627…` |
| Firmware zip | `~/whoop-re/firmware/whoop5_nordic_3.30.5.0.zip` | sha256 `5074b82a38da6718…` |
| Firmware image | `~/whoop-re/firmware/extracted/usb_update_puffin_3.30.5.0_5ce45820.bin` | 410,019 B, sha256 `5230866c0a34ad7d…` |

## The chain, with evidence

1. **APK pull** — REA `pull_adb_package` (device <adb-serial>, package
   `com.whoop.android`). REA `inspect_artifact` inventory Evidence:
   `ev_b8889b93eae61434684454c088608a72a4027af09c36b38dbcad43a33d3a431a`
   (4,952 entries, complete, 0 integrity contradictions).
2. **Endpoint discovery** (jadx 1.5.2 CLI, sources under `~/whoop-re/jadx-*`):
   - `OtaFirmwareUpdateApi` (`com.whoop.firmwareUpdateService.data.api`):
     - `POST firmware-service/v4/firmware/check?deviceName={DEV}` — body
       `[{chip_name, version}]` → `DesiredDeviceFirmwareConfig{chip_firmwares,…}`
     - `POST firmware-service/v4/firmware/version?deviceName={DEV}` — body
       `DownloadFwFilePayload{current_chip_firmwares, chip_firmwares_of_upgrade}`
       → `DesiredDeviceFirmwareUpdate{desired_device_firmware_config, firmware_zip_file}`
     - `POST firmware-service/v4/firmware/result` (untested)
   - **ChipName enum** (from a server 400 error): `[MAXIM, NORDIC, PEARL, RUGGLES, AMBIQ]`
   - Device codenames: `PUFFIN` (5.0/MG), `HARVARD` (4.0), `LABRADOR`
   - Required headers: `X-WHOOP-Device-Platform: Android`, `X-WHOOP-Device-OS`,
     `X-WHOOP-Device-Model`
   - API base: `https://api.prod.whoop.com`
3. **Auth** (the hard part):
   - App login = AWS Cognito via proxy `POST /auth-service/v3/whoop`
     (X-Amz-Target JSON), app client id `5311fm7hqak9lrnpht6kp2p5uh`
     (`yi0/c.java`). Token exchange for app services:
     `POST auth-service/v2/whoop/refresh` with `Authorization: Bearer <cognito
     access token>` → `AuthMaterial{access_token,…}`.
   - App stores tokens **Keystore-encrypted** (`encryptedAccessToken` in
     `files/datastore/complete_data_store_file.preferences_pb`,
     `auth_material_key_set` in `auth_material_prefs_name.xml`).
   - Direct password login blocked: wrong credentials in our case *and*
     Cloudflare JA3-blocking of curl (403). `curl_cffi`
     `impersonate="chrome124"` passes Cloudflare.
   - **Emulator route** (no device mod): official Android SDK emulator
     (API 34 google_apis x86_64, AVD `whoop`), `adb root`, installed the
     genuine split APKs (`install-multiple`; ARM translation ran the arm64
     libs fine), user logged in normally, then **frida-server 16.5.9
     (x86_64)** + spawn-mode hook on `javax.crypto.Cipher.doFinal` captured
     the decrypted access JWT. (Process name is `WHOOP`, not the package id.
     frida 17.x dropped the bundled Java bridge — use 16.x.)
   - Working bearer = **Cognito access token** (claims: iss
     `cognito-idp.us-west-2.amazonaws.com/us-west-2_rYv1jh`, RS256, exp
     epoch 1792183175, client_id `37365lrcda1js3fapqfe2n…` — differs from the
     in-app client id; issued via the browser/Google flow). A 422-char JWE
     capture was rejected (401) — likely the id/refresh token, not the API
     bearer.
4. **Firmware protocol quirks** (all observed):
   - `/check` accepts version `0.0.0` for "what's sanctioned".
   - `/version` 404s ("not the sanctioned target") unless
     `chip_firmwares_of_upgrade` equals the sanctioned set from `/check`
     and `current_chip_firmwares` differs (we sent `1.0.0` → `3.30.5.0`).
   - `firmware_zip_file` is **base64 ZIP content inline**, not a URL.
   - Sanctioned at capture time: `NORDIC 3.30.5.0` for `PUFFIN`.
5. **Firmware image triage** (manual; REA `open_binary` rejects the raw
   blob — no recognized container/OS header, recorded as limitation):
   - Header: magic `3d b8 f3 96`, +8 flags? `00 02 00 00`, +12 LE length
     `0x00063F0C` (409,356 B payload), then 0xFF flash padding → raw
     nRF52 (Cortex-M) image, **unencrypted**.
   - Entropy: ~6.3–6.9 bits/byte body, 3.3 tail (not compressed as a whole).
   - Notable strings: `mcuboot_util`, `memfault_*`, `Puffin`, `3.30.5.0`,
     `whoop_adc`, `whoop_bt_info_msg`, `whoop_mfg_msg`,
     `whoop_interface_incoming_packet_verify` ("Wrong Packet Size",
     "Header CRC ERR"), charger `MXC3500AL`, LED `lp5562`,
     `SW version: %s` / `HW version: %s`.

## Scripts (in `~/whoop-re/`, not yet in repo)

- `fw_with_token.py` — token → check → version → decode base64 zip → save
- `frida_spawn_dump.py` / `frida_spawn_dump2.py` — spawn + Cipher hook capture
- `whoop_fw_fetch.py` — password-login route (works if credentials correct;
  includes MFA challenge handling)
- Secrets: `tokens.jsonl`, `auth_material.xml` (chmod 600; rotate/delete)

## Open items

- [ ] Ghidra pass on the `.bin` (needs `GHIDRA_INSTALL_DIR`; ARM/Cortex-M,
      MCUboot) → extract BLE opcode/message tables for `whoop/ble/protocol.py`
- [ ] `PEARL`/`RUGGLES`/`AMBIQ`/`MAXIM` firmware: `/check` only returned
      NORDIC; retry with real current versions from a paired strap
- [ ] `firmware-service/v4/firmware/result` semantics (report-back after OTA)
- [ ] Port `fw_with_token.py` into repo as `whoop firmware fetch` + tests
- [ ] Token lifecycle: capture expires; refresh via emulator flow or correct
      password login

## Legal

Same stance as the repo: own account, own devices, interoperability research
(GDPR Art. 20 rationale). No credential or token committed to the repo.
