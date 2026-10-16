# E2E Full Data Export — Reverse Engineering Plan (WHOOP 5.0)

Goal: decrypt WHOOP's end-to-end-encrypted "full data export" locally and turn
it into a third data source for this repo (`whoop export …`).

Everything below is a hypothesis until verified against artifacts.

## Checklist

| # | Question | Evidence needed | Status |
|---|---|---|---|
| 1 | What container is the export? (ZIP-AES, custom, etc.) | export file, `open_binary` inventory | ⬜ blocked: no export file |
| 2 | Where does the key come from? (shown passphrase vs account-derived) | APK code: KDF + key source | ✅ **answered**: app has NO export crypto — no KDF, no Cipher use, no passphrase strings anywhere in the 11 dexes. Archive + key must arrive out-of-band (server emails export to account address). |
| 3 | Cipher + KDF params (AES-GCM/CBC? PBKDF2/Argon2? iterations/salt) | `inspect_android_method` on crypto path | ✅ **n/a on-device** — only AES constants in app are obfuscated 3rd-party libs (Iterable analytics). Container crypto TBD from the export file itself. |
| 4 | What is inside once decrypted (schemas/tables/files)? | decrypted export | ⬜ blocked on 1–3 |
| 5 | Does crypto live in native `.so`? | APK `lib/` inventory | ✅ **no** — split arm64 libs are WebRTC/Mapbox/QuickJS/Sentry/Seon only; no custom crypto lib |
| 6 | Decoder integrated as `whoop export decrypt` + tests | this repo | ⬜ |

## REA toolchain status (audited this session, Linux host)

- ✅ `open_binary`, `inspect_artifact`, `extract_artifact`, graph projection,
  `compare_application_versions`
- ❌ Android code inspection (`inspect_android_package`,
  `search_android_classes`, `inspect_android_class/method`,
  `trace_android_references`): JADX missing → install Java + jadx, set
  `REA_JADX_COMMAND`, restart agent
- ❌ `decode_android_resources`: apktool missing → optional,
  `REA_APKTOOL_COMMAND`
- ❌ Ghidra (`GHIDRA_INSTALL_DIR` 12.1.x): only if #5 shows native crypto

## Required from operator

1. WHOOP Android APK (own device via `adb shell pm path <pkg>` + `adb pull`,
   or APKMirror; version-matched) → `~/whoop-re/`
2. Personal E2E full-data export from the WHOOP app + the passphrase the app
   displays
3. `sudo apt install -y openjdk-21-jre-headless`; jadx release unzipped;
   `REA_JADX_COMMAND=/abs/path/jadx/bin/jadx`

## Evidence log (2026-10-16)

- APK set pulled via REA `pull_adb_package` from device <adb-serial>:
  `base.apk` sha256 `c9b50724cad2e7d1…`, `split_config.arm64_v8a.apk` `978a8cbd2f8c4627…`,
  `split_config.xxhdpi.apk` `14e770f0a513800d…` → `~/whoop-re/com.whoop.android/`
- REA `inspect_artifact` inventory: `ev_b8889b93eae61434684454c088608a72a4027af09c36b38dbcad43a33d3a431a`
  (4,952 entries, complete, 0 integrity contradictions)
- Export request API (jadx, `jadx-export/sources/com/whoop/memberdataexport/services/network/MemberDataExportServiceApi.java`):
  - `GET /member-data-export-service/v1/member-data-export-details`
  - `POST /member-data-export-service/v1/member-data-export-details` body `SubmitMemberDataDto{email}`
  - Response `IntermediateMemberDataExportDto` is UI copy + `recipient_email_display` + `state`
    → server emails the export to the account address; app never sees key material
- Download path (jadx): `DownloadAction{path, filename, timeoutMillis}` → apiClient →
  `FileExportUtil` (`whoopaction/view/a.java`) = plain byte copy to SAF Uri,
  `application/octet-stream`. No decrypt step.
- Only zip handling in app: firmware-update-service (`of1/b.java`). Unrelated.
- jadx 1.5.2 CLI at `~/jadx` (REA MCP android provider still needs `REA_JADX_MCP_JAR`
  + restart — optional; CLI sufficed).

## Remaining open item

- Q1/Q4/Q6: need the operator's actual export archive. Trigger in app, wait for
  email, download, copy to `~/whoop-re/`. Identify container (ZipCrypto vs
  WinZip-AES vs 7z/custom) from the file, note where the password is delivered
  (email body? download page?), then build `whoop export decrypt`.

## Related

- Firmware fetch RE (same session, completed): [`firmware_fetch.md`](firmware_fetch.md)
  — auth chain, firmware-service v4 protocol, NORDIC 3.30.5.0 image captured.

## Legal

Same stance as the rest of this repo: personal data, own device and account,
interoperability/GDPR Art. 20 rationale. Not affiliated with WHOOP.
