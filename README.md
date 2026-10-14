# droidvault

A guided, verified backup of an Android phone over ADB, for use before a wipe or a custom-ROM clean flash.

You run one Python file on your computer. It interviews you about the things only you can do inside apps.
It then streams everything else to your computer over USB and checks every archive against the device.
The result is a single folder holding a summary, a "what was NOT backed up" list, and a restore guide.

- **Cross-platform:** macOS, Linux and Windows. Python ≥ 3.8 standard library only, plus `adb`.
- **Cross-device:** it detects the root solution (KernelSU, Magisk or APatch), the partition layout
  (MediaTek or Qualcomm), work profiles (Island, Shelter, etc.), and which messenger, authenticator and banking apps are installed.
- **Root-aware:** it works without root too, in a reduced mode (shared storage + inventory).
- **Read-only on the phone:** the only writes are a scratch dir in `/data/local/tmp` and
  "stay awake while plugged in". Both are reverted at the end.
- **Resumable:** re-run the same command after an interruption and it skips archives already done.

---

## Why this exists

A normal "backup before flashing" misses things. These are the ones that hurt:

| Commonly missed | Why it hurts | What droidvault does |
|---|---|---|
| 2FA / authenticator apps | Their data is locked by the **hardware keystore**, so restoring the files doesn't work. You get locked out of accounts. | Detects them and puts a manual export step on the checklist. Copies the data as *reference only*, and `restore-app` refuses to restore it. |
| Banking / UPI apps | Bound to the device and keystore. | Checklist item: know your PINs, keep the bound SIM. Not restored from files. |
| WhatsApp **in a work profile** (a second number) | Nobody remembers it's there. | Detects WhatsApp / WA Business in **every** user and backs up each one: media, local `crypt14/15` backup, and the key file. |
| Call recordings / app files in `Android/data` | A wipe deletes `Android/data`, and many backup apps can't read it on Android 11+. | Copied with root, and listed separately so you can check it. |
| SMS, call log, alarms, blocked numbers | Stored in system databases, not in the cloud. | Copies the databases **and** writes plain-text exports. |
| WiFi passwords, Bluetooth pairings, ADB keys | Annoying to rebuild. | Copied, plus a plain `SSID ↔ password` text list. |
| IMEI / RF calibration (MediaTek `nvram/nvdata`, Qualcomm `modemst*/fsg/efs`, `persist`) | If a flash corrupts them you lose cellular, and only a backup from **this exact phone** fixes it. | Images every identity/calibration partition with sha256 verification. Also takes a file-level copy of the live-mounted ones. |
| Root setup (keybox, TrickyStore, PIF, module configs, LSPosed scopes) | Hours of tuning. | Archives all of `/data/adb` and turns the LSPosed scopes into a readable table. |
| Files you left in `/data/local/tmp` | A wipe deletes them. | Archived. |
| The current boot chain | Needed if you want to roll back. | Images `boot`, `init_boot`, `vendor_boot`, `dtbo`, `vbmeta*`, etc. for the active slot. |
| "I think it's in the backup" | Truncated streams and files that changed mid-copy. | Every archive is hashed while it streams, re-read, and its entry count compared with the device. A final check asserts that each critical item that **exists on the phone** is in some archive. |

---

## Requirements

- Python 3.8+ (`python3 --version`)
- Android platform-tools (`adb`) in your PATH, or pass `--adb /path/to/adb`
- USB debugging enabled and the computer authorised
- **Root** for the full backup: grant root to the **Shell** app in KernelSU, Magisk or APatch
- Free disk space roughly equal to the phone's used storage. The tool estimates it for you.

## Quick start: one command, every time

```bash
git clone https://github.com/devporter007/droidvault && cd droidvault
python3 droidvault.py
```

That's the only command you need. **Run the same thing every time** and it works out what to do:

| Situation | What happens |
|---|---|
| `adb` not installed | It tells you the exact install command for your OS. |
| Phone not plugged in / USB debugging off / "Allow USB debugging?" not tapped | It waits and tells you what to do on the phone. It doesn't crash. |
| Root not granted to Shell | It tells you where to tap in KernelSU/Magisk/APatch and re-checks. It can also continue without root. |
| `~/.droidvault/profiles/<your phone's codename>.json` exists | It's used automatically (`--profile` is optional). |
| First run | Full backup (15–30 min). You pick extra apps once. |
| Any later run | **Continues the latest backup** and copies only what changed (about 1–2 min). |
| You edited the profile (added an app, a check, …) | Picked up on the next run. New apps get copied. Items that no longer apply drop off the checklist. |
| Checklist | Only asks items that are still open. Items already done are listed and skipped. |
| WhatsApp | It reads the **backup file on the phone**. If it's older than 3 hours, it asks you to tap Back up, then re-checks when you press Enter. A run made after the backup automatically refreshes the copy of it. |
| Second copy | Asked **once**: drag an (encrypted) drive into the window. Every later run syncs it automatically. On macOS it warns if the drive isn't encrypted. |
| End of run | A plain verdict: **✅ SAFE TO WIPE**, or **⛔ NOT YET** with the list of what's left. "Run the same command again" is always the next step. |
| Ctrl-C / unplugged mid-run | Run the same command again. It picks up where it stopped. |

Occasional extras: `--pick-apps` (add apps to back up), `--new` (start a separate backup folder), `--deep` (re-hash everything).
The output goes to `~/android-backups/<codename>-<date-of-first-run>/`.

## The phases

Run them all (default), or pick some with `--only a,b,c`. `preflight` always runs.

| # | Phase | What it does | Needs root |
|---|---|---|---|
| 1 | `preflight` | Finds the device, root, `tar`, partitions, users and slot. Checks battery and free disk space. | – |
| 2 | `checklist` | An interactive to-do list **built from what's installed**: WhatsApp per user, each authenticator, Google account count, Photos, Chrome, Telegram, Signal, banking apps, LSPosed, new-ROM files, charge. Answers are saved. | – |
| 3 | `inventory` | Saves app lists (per user, with installer + version), settings (global/secure/system), accounts, default-app roles, root modules, TrickyStore, the partition table, data sizes, and LSPosed scopes as a table. This is your rebuild checklist. | partly |
| 4 | `partitions` | `dd` of the identity/calibration partitions and the active boot chain, sha256-verified (up to 3 retries on live partitions). Also a file-level tar of `/mnt/vendor/*` and `/efs`. | yes |
| 5 | `system` | Archives `/data/adb` (root setup), `/data/local/tmp`, WiFi, Bluetooth, ADB keys, VPN, sync, per-user system settings, and the telephony/contacts/blocked-number/calendar/clock/launcher providers. | yes |
| 6 | `exports` | Plain-text SMS, MMS index, call log, blocked numbers, contacts, and WiFi SSID/password list. | partly |
| 7 | `storage` | **Gate first:** if WhatsApp is installed and its in-app backup isn't confirmed in the checklist, it stops and asks (mark done / continue with a stale copy / stop). Then it copies internal storage of user 0, split into `sd-messengers`, `sd-camera`, `sd-download`, `sd-android-data` and `sd-rest`, so a single part can be redone. | `Android/data` only |
| 8 | `profiles` | Each secondary user / work profile: its media and all of its app data (minus Play Store cache). | yes |
| 9 | `apps` | Data for the selected apps (profile list + an interactive pick from a size-sorted list), keystore-bound apps as reference, WhatsApp key files, and APKs for sideloaded apps + your `always_apks`. | data: yes |
| 10 | `verify` | Re-hashes every archive and partition image. Checks critical items (device → archive). Writes `NOT_BACKED_UP.txt`. | – |
| 11 | `report` | Asks the **before-you-wipe** steps that need the finished backup: review `NOT_BACKED_UP.txt`, make a second encrypted copy, and refresh a stale WhatsApp copy if there is one. Then gives a verdict, **✅ SAFE TO WIPE** or **⛔ NOT YET SAFE TO WIPE** with the open items. Writes `SUMMARY.md` and `RESTORE.md`, copies the tool + profile into the backup, and cleans up the device. | – |

### Options

```
-s SERIAL        pick a device when several are connected
-o DIR           output directory
--adb PATH       adb binary
--profile FILE   device/user profile (JSON)
--only a,b       run only these phases
--yes            no prompts; checklist items recorded as UNCONFIRMED (the verdict stays NOT YET SAFE)
--redo           refresh archives that already exist
--all-apps       back up data for every user app, not just the selection
--all-apks       save APKs for every user app, not just sideloaded ones
--super          also image `super` (system/vendor/product). Large; only for exact rollback
--deep           (update) re-hash every archive, not just this run's changes
--phases         list phases
```

---

## Updating a backup (low effort)

Updating is automatic: just run `python3 droidvault.py` again. (`python3 droidvault.py update` does the same and never starts a new backup.)

- **No hunting for the folder.** It picks the newest `~/android-backups/<codename>-*` backup (or pass `-o DIR`).
- **Copies only differences.** It indexes the phone (path, size, mtime) and diffs that against what the backup holds.
  New and changed files go into a small numbered update next to the original:
  `archives/<name>.d/001-<timestamp>.tar`, plus a `.deleted` list for files that have gone from the phone.
  Unchanged archives print `unchanged` and cost nothing.
- **Few questions.** It skips the checklist (your answers are kept) and the app picker (your selection is kept).
  It only asks the before-wipe items that are still open. If anything changed, it re-opens
  "second, encrypted copy", because that copy is now out of date.
- **Partitions** are compared by sha256 on the phone and re-imaged only if they differ. **APKs** are compared by size + mtime,
  so an app update re-downloads only that app.
- **Verification** re-hashes this run's updates and size-checks everything else. `--deep` re-hashes the whole backup.
- **Speed:** on the reference device (17 GB backup) a routine update takes ~1.5 min. The very first update after
  a full backup is slower: it builds the file index once from the existing archives.
- **Starting fresh:** `--redo` makes a new full copy of the archives you name with `--only`. Their old updates are dropped.

Getting the latest state back out of an archive with updates (on the computer):

```bash
python3 droidvault.py extract ~/android-backups/<device>-<date> sd-camera ./restored-camera
```

`restore-app` applies the original archive, every update in order and the deletions automatically.

### When is it safe to wipe?

Only when the report ends with **✅ SAFE TO WIPE**. That needs both of these:
1. `verify` passed: every archive re-hashed, every critical item that exists on the phone found in an archive.
2. Every checklist item is DONE or N/A. That includes the in-app steps (authenticators, WhatsApp, Google accounts, …)
   and the before-you-wipe steps (`NOT_BACKED_UP.txt` reviewed, second encrypted copy made).

If an item is still open, do it and run the same command again: it only asks what is still open.
WhatsApp archives made before you confirmed its backup are marked stale and refresh on the next `storage`/`profiles` run.

Output is line-buffered, so `python3 droidvault.py … > run.log` can be followed live with `tail -f run.log`.

## The output folder

```
<device>-<date>/
├── SUMMARY.md            archives table, partition table, checklist status, warnings
├── RESTORE.md            step-by-step restore, plus your profile's restore_notes
├── NOT_BACKED_UP.txt     apps with ≥20 MB data that were NOT backed up: review it
├── WARNINGS.txt          anything that went wrong this run
├── state.json            manifest (size, sha256, entry counts) + checklist answers; drives resume
├── device.json           serial, fingerprint, root, users, slot
├── backup.log
├── archives/             *.tar + *.list (listing) + *.index.json (size/mtime per file) + *.errors.txt
│   ├── <name>.d/         incremental updates: 001-<ts>.tar, 001-<ts>.deleted, 002-…
│   ├── root-adb.tar  local-tmp.tar  system-state.tar  nv-mounted-files.tar
│   ├── sd-messengers.tar  sd-camera.tar  sd-download.tar  sd-android-data.tar  sd-rest.tar
│   ├── user10-media.tar  user10-apps.tar          (one pair per secondary user)
│   └── app-<package>.tar                          (one per selected app)
├── partitions/           <name>.img (sha256 in state.json) + README.txt
├── apks/<package>/*.apk  base + split APKs, sha256-checked against the device
├── messenger-keys/       WhatsApp / WA Business `key` and `encrypted_backup.key` per user
├── exports/              sms.txt  call_log.txt  contacts_flat.txt  wifi_networks.txt  …
├── inventory/            package lists, settings, accounts, modules, lspd_scopes.txt, …
├── droidvault.py         the exact tool version that made this backup
└── profile.json
```

Archives are plain `tar` files with paths relative to `/` (e.g. `data/media/0/DCIM/…`). Open them with any tar tool, 7-Zip on Windows included.

### Reading the verification

- `ok name.tar … N entries verified`: the archive's entry count equals the count of files on the device (caches excluded).
- `+N runtime sockets, not archivable`: Unix sockets (e.g. from Zygisk) can't be stored in tar. Expected.
- `COUNT-MISMATCH`: a file changed or vanished during the copy, usually because an app was running.
  Close the app and use `--only <phase> --redo`.
- `changes while mounted — not bit-exact`: the partition is written live (MediaTek `nvcfg`/`nvdata`).
  The image is kept, and `nv-mounted-files.tar` holds a consistent file-level copy.
- `MISSING: X — exists on device but in no archive`: a real gap. Fix it before wiping.

---

## Restoring: also one command

After flashing, connect the phone and run:

```bash
python3 droidvault.py restore            # finds the latest backup of this phone by itself
python3 droidvault.py restore --dry-run  # see exactly what it would do, writes nothing
```

It works like the backup run: it checks the phone's real state at every step, does what it safely can, and pauses
for things only you can do. Re-run it any time; finished steps are skipped.

| # | Step | What it does | You do |
|---|---|---|---|
| 1 | Check the phone | Compares it with the backup. Warns if it's a different handset or model. | confirm |
| 2 | Root access | Waits until root works for Shell. | flash root for the **new** kernel |
| 3 | Root modules | Lists the modules from your old setup that aren't installed yet, and re-checks. | install the zips |
| 4 | Root config | Puts back keybox / TrickyStore target / security patch / PIF config with original owner + mode. Skips files that are already identical. | – |
| 5 | LSPosed | Installs your module APKs from the backup. Compares the phone's **enabled scopes** with the backup's and lists exactly what to tick. | install the manager, tick scopes |
| 6 | WiFi / Bluetooth / ADB key | Turns radios off, restores the config files with their original owner/mode, reboots, turns radios back on. | – |
| 7 | Photos & files | Unpacks the media archives (with all updates applied) and `adb push --sync`es them to `/sdcard`. `Android/data` only goes back for apps that are installed. | – |
| 8 | WhatsApp (each account, incl. work profile) | Waits until it's installed but not yet opened, copies the backup + media into place, then waits for your chats to appear. | install, then tap Restore |
| 9 | Messages + call log | Counts what's on the phone against the backup. | restore the XML in SMS Backup & Restore |
| 10 | App data | Installs saved APKs if missing, restores data with the new UID + SELinux labels. Refuses keystore-bound apps. | install Play-only apps first |
| 11 | Accounts + logins | Checklist: authenticators, Google accounts, banking/UPI, other apps. | log in |

Single pieces still work on their own: `restore-app <backup> <pkg> [--user N]` and `extract <backup> <archive> <dest>`.
**Partition images (IMEI/calibration) are never written automatically.** Use them only if signal/IMEI is gone, and only on the same handset.

## Profiles

A profile is optional JSON that holds everything specific to one device or one user, so the tool itself stays generic.
Start from `profiles/example.json`.

| key | meaning |
|---|---|
| `curated_apps` | Apps whose data is backed up by default (safe to raw-restore: no keystore dependency). |
| `reference_apps` | Extra keystore-bound apps to copy as reference only (added to the built-in list). |
| `banking_apps` | Added to the auto-detected banking/UPI checklist item. |
| `always_apks` | APKs always saved (patched apps, apps that misreport their installer, hard-to-find versions). |
| `extra_partitions` | Additional by-name partitions to image. |
| `must_have` | `[label, regex]` pairs checked against the archive listings in `verify`. |
| `manual_items` | `[title, line, line…]` extra checklist entries. |
| `restore_notes` | Lines appended to `RESTORE.md`. |

**Where profiles live:** put yours in `~/.droidvault/profiles/<codename>.json` (find the codename with
`adb shell getprop ro.product.device`). It's loaded automatically, and because it sits outside the repo it can't end up
in a commit by accident. Profiles tend to hold personal details (your app list, banking apps, hidden package names),
so keep them private. `profiles/` in the repo is also checked, but `.gitignore` excludes everything there except the example.

---

## Limitations

- **Keystore-bound data cannot be restored from files**, by design of Android. Use each app's own export.
- Without root you get shared storage (minus `Android/data` on Android 11+) and the inventory only.
- Live databases (messengers) may change during the copy. Close the apps, or refresh with `--redo`.
- If the tool can't find busybox, it uses the phone's built-in toybox `tar`. That `tar` matches cache-exclusion patterns slightly
  differently, which can produce harmless COUNT-MISMATCH warnings.
- Restoring a system database from one Android version onto another (SMS, call log) may not work. That's why the plain-text and XML exports exist.

## Security

A backup contains WiFi passwords, messenger keys, auth tokens inside app data, and your keybox / attestation material.
**Keep it on an encrypted disk** (FileVault, BitLocker, LUKS) or in an encrypted archive. Don't sync it to cloud storage in the clear.

## License

MIT. See [LICENSE](LICENSE).
