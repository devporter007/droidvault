#!/usr/bin/env python3
"""droidvault — guided, verified, pre-wipe backup of an Android device over ADB.

Cross-platform (macOS / Linux / Windows), stdlib-only, Python 3.8+. Needs `adb` in PATH.
Read-only on the device apart from a scratch dir in /data/local/tmp. Root (KernelSU,
Magisk, APatch) unlocks app data, system state and partitions; without root it falls
back to shared storage + inventory only.

  python droidvault.py                          full guided run (resumable)
  python droidvault.py --profile myphone.json   (profiles: ~/.droidvault/profiles/<codename>.json, auto-loaded)
  python droidvault.py --only storage,verify    run selected phases
  python droidvault.py update --profile …      add only what changed to the latest backup
  python droidvault.py --phases                 list phases
  python droidvault.py extract <backup-dir> <archive> <dest>     latest state of one archive
  python droidvault.py restore-app <backup-dir> <pkg> [--user N]   (after the wipe)

Every archive is streamed straight to the computer, sha256'd in flight, re-read, and its
entry count compared against the device. `verify` then checks that every critical item
that exists on the device is present in some archive, and lists what was NOT backed up.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

if sys.version_info < (3, 8):
    sys.exit("droidvault needs Python 3.8 or newer (python3 --version)")

DTMP = "/data/local/tmp/droidvault"
PRUNE = ["cache", "code_cache", ".transforms", ".picker_transcoded"]
PHASES = ["preflight", "checklist", "inventory", "partitions", "system", "exports",
          "storage", "profiles", "apps", "verify", "report"]

# Partitions holding per-handset identity/calibration (IMEI, RF cal, DRM, sensors).
# MediaTek, Qualcomm and generic names; whatever exists is imaged.
NV_PARTS = ["nvram", "nvdata", "nvcfg", "proinfo", "protect1", "protect2", "persist", "persistbak",
            "seccfg", "otp", "efuse", "para", "misc", "frp",
            "modemst1", "modemst2", "fsg", "fsc", "efs", "sec", "devinfo", "keystore", "ddr"]
BOOT_PARTS = ["boot", "init_boot", "vendor_boot", "dtbo", "vbmeta", "vbmeta_system",
              "vbmeta_vendor", "recovery", "lk", "logo"]
BIG_PART_LIMIT = 512 * 1024 * 1024

# Apps whose local data is bound to the hardware keystore: back up as reference only.
KEYSTORE_BOUND = ["com.whatsapp", "com.whatsapp.w4b", "org.thoughtcrime.securesms",
                  "io.ente.auth", "com.authy.authy", "com.google.android.apps.authenticator2",
                  "com.azure.authenticator", "com.okta.android.auth", "com.duosecurity.duomobile",
                  "com.lastpass.authenticator", "com.x8bit.bitwarden", "com.onepassword.android"]
AUTHENTICATORS = ["io.ente.auth", "com.authy.authy", "com.google.android.apps.authenticator2",
                  "com.azure.authenticator", "com.beemdevelopment.aegis", "org.fedorahosted.freeotp",
                  "com.okta.android.auth", "com.duosecurity.duomobile", "com.x8bit.bitwarden",
                  "com.lastpass.authenticator", "proton.android.pass", "com.yubico.yubioath"]
BANK_HINT = re.compile(r"bank|\bupi\b|bhim|paisa|phonepe|paytm|walletnfc|\.wallet\b|groww|zerodha|kite|\.cred\b|cheq|"
                       r"(^|\.)pay(\.|$)|\.payments?\b", re.I)

# Things that, if they exist on the device, MUST end up in some archive. {u} = each user id.
CRITICAL = [
    ("KernelSU/Magisk/APatch module data", r"data/adb/modules/"),
    ("TrickyStore keybox", r"data/adb/tricky_store/keybox\.xml$"),
    ("TrickyStore target list", r"data/adb/tricky_store/target\.txt$"),
    ("PlayIntegrityFix config", r"data/adb/(pif\.(prop|json)|modules/playintegrityfix/(custom\.)?pif\.(prop|json))$"),
    ("LSPosed module config", r"data/adb/lspd/config/modules_config\.db$"),
    ("WiFi networks", r"data/misc/(apexdata/com\.android\.wifi|wifi)/WifiConfigStore\.xml$"),
    ("Bluetooth pairings", r"data/misc/bluedroid/bt_config\.conf$"),
    ("ADB authorised keys", r"data/misc/adb/adb_keys$"),
    ("SMS/MMS database", r"com\.android\.providers\.telephony/databases/mmssms\.db$"),
    ("Call log database", r"com\.android\.providers\.contacts/databases/calllog\.db$"),
    ("Alarms", r"deskclock/databases/alarms\.db$"),
    ("WhatsApp local backup (user {u})", r"data/media/{u}/Android/media/com\.whatsapp/WhatsApp/Databases/msgstore\.db\.crypt1\d$"),
    ("WhatsApp Business local backup (user {u})", r"data/media/{u}/Android/media/com\.whatsapp\.w4b/WhatsApp Business/Databases/msgstore\.db\.crypt1\d$"),
    ("Camera roll (user {u})", r"data/media/{u}/DCIM/Camera/[^/]+$"),
    ("Screenshots (user {u})", r"data/media/{u}/(Pictures|DCIM)/Screenshots/[^/]+$"),
    ("Voice/call recordings (user {u})", r"data/media/{u}/(Recordings|Music/Recordings|Call|MIUI/sound_recorder)/.+"),
    ("Documents (user {u})", r"data/media/{u}/Documents/.+"),
]


# ------------------------------------------------------------------------------ UI
class UI:
    def __init__(self):
        on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        if on and os.name == "nt":
            os.system("")  # enable ANSI on Windows 10+
        c = (lambda code: f"\033[{code}m") if on else (lambda code: "")
        self.B, self.R, self.G, self.Y, self.C, self.N = c(1), c(31), c(32), c(33), c(36), c(0)
        self.log_path = None
        self.warn_path = None
        self.yes = False

    def _log(self, kind, msg):
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now():%F %T} {kind} {msg}\n")

    def hdr(self, msg):
        print(f"\n{self.B}━━ {msg} ━━{self.N}"); self._log("PHASE", msg)

    def say(self, msg):
        print(f"{self.C}==>{self.N} {msg}"); self._log("INFO", msg)

    def ok(self, msg):
        print(f"  {self.G}ok{self.N}   {msg}"); self._log("OK", msg)

    def warn(self, msg):
        print(f"  {self.Y}warn{self.N} {msg}"); self._log("WARN", msg)
        if self.warn_path:
            with open(self.warn_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")

    def die(self, msg):
        print(f"{self.R}error:{self.N} {msg}", file=sys.stderr); self._log("FATAL", msg); sys.exit(1)

    def progress(self, label, n):
        if not sys.stdout.isatty():
            return
        sys.stdout.write(f"\r  {self.C}…{self.N}    {label}  {human(n)}   "); sys.stdout.flush()

    def clear(self):
        if not sys.stdout.isatty():
            return
        sys.stdout.write("\r" + " " * 90 + "\r"); sys.stdout.flush()

    def ask(self, prompt, default=""):
        if self.yes:
            return default
        try:
            return input(f"  {prompt} ").strip()
        except EOFError:
            return default


ui = UI()


def human(n):
    n = float(n)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}"
        n /= 1024


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -------------------------------------------------------------------------- Device
class Device:
    def __init__(self, adb, serial=None):
        self.adb = adb
        self.serial = serial or self._pick_serial()
        self.root = False
        self.tar = "tar"
        self.byname = None
        self.slot = ""
        self.users = [0]

    def _pick_serial(self):
        """Wait (with plain-language hints) until exactly one usable phone is connected."""
        last = None
        while True:
            out = subprocess.run([self.adb, "devices"], capture_output=True, text=True).stdout
            rows = [l.split() for l in out.splitlines()[1:] if l.strip()]
            ready = [r[0] for r in rows if r[1] == "device"]
            if len(ready) == 1:
                return ready[0]
            if len(ready) > 1:
                print("  Several phones are connected:")
                for i, s_ in enumerate(ready, 1):
                    print(f"   {i}. {s_}")
                a = ui.ask("Which one? (number):", "1")
                return ready[int(a) - 1] if a.isdigit() and 0 < int(a) <= len(ready) else ready[0]
            if any(r[1] == "unauthorized" for r in rows):
                msg = "Unlock your phone and tap ALLOW on the 'Allow USB debugging?' popup (tick 'Always allow')."
            elif any(r[1] == "offline" for r in rows):
                msg = "The phone is connected but offline. Unplug it and plug it back in."
            else:
                msg = ("Plug the phone in with a USB cable. If it's already plugged in: Settings › System › "
                       "Developer options › turn on USB debugging.")
            if ui.yes:
                ui.die(msg)
            if msg != last:
                print(f"  {ui.Y}waiting for the phone…{ui.N} {msg}  (Ctrl-C to quit)")
                last = msg
            time.sleep(2)

    def _argv(self, cmd, root):
        # base64 transport: no quoting hazards on any host shell; exec-out: binary-safe stdout.
        b64 = base64.b64encode(cmd.encode()).decode()
        inner = f"echo {b64} | base64 -d | sh"
        return [self.adb, "-s", self.serial, "exec-out", f"su -c '{inner}'" if root else inner]

    def sh(self, cmd, root=None, binary=False, timeout=None):
        root = self.root if root is None else root
        r = subprocess.run(self._argv(cmd, root), capture_output=True, timeout=timeout)
        return r.stdout if binary else r.stdout.decode("utf-8", "replace").replace("\r\n", "\n")

    def exists(self, path):
        return self.sh(f'[ -e "{path}" ] && echo y').strip() == "y"

    def stream(self, cmd, dest, label):
        """Stream stdout of a device command into dest; returns (bytes, sha256, rc)."""
        tmp = Path(str(dest) + ".partial")
        p = subprocess.Popen(self._argv(cmd, self.root), stdout=subprocess.PIPE)
        h, n, t = hashlib.sha256(), 0, 0.0
        with open(tmp, "wb") as f:
            while True:
                chunk = p.stdout.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk); h.update(chunk); n += len(chunk)
                if time.time() - t > 0.5:
                    ui.progress(label, n); t = time.time()
        rc = p.wait()
        ui.clear()
        tmp.replace(dest)
        return n, h.hexdigest(), rc

    def detect(self):
        self.root = self.sh("id -u", root=True).strip() == "0"
        if self.root:
            for bb in ["/data/adb/ksu/bin/busybox", "/data/adb/magisk/busybox", "/data/adb/ap/bin/busybox"]:
                if self.exists(bb):
                    self.tar = f"{bb} tar"; break
            self.byname = self.sh("for d in /dev/block/by-name /dev/block/bootdevice/by-name; do "
                                  "[ -d $d ] && echo $d && exit; done; "
                                  "find /dev/block/platform -maxdepth 3 -type d -name by-name 2>/dev/null | head -1").strip() or None
        self.slot = self.sh("getprop ro.boot.slot_suffix", root=False).strip()
        users = re.findall(r"UserInfo\{(\d+):", self.sh("pm list users", root=False))
        self.users = [int(u) for u in users] or [0]

    def media(self, user):
        return f"data/media/{user}" if self.root else f"storage/emulated/{user}"

    def packages(self, user=0, third_party=True):
        out = self.sh(f"pm list packages {'-3' if third_party else ''} -i --user {user}", root=False)
        res = {}
        for l in out.splitlines():
            m = re.match(r"package:(\S+)\s+installer=(\S+)", l)
            if m:
                res[m.group(1)] = m.group(2)
        return res


# -------------------------------------------------------------------------- Backup
class Backup:
    def __init__(self, dev, out, args, profile):
        self.dev, self.out, self.args, self.profile = dev, out, args, profile
        self.arch = out / "archives"
        self.arch.mkdir(parents=True, exist_ok=True)
        self.state_path = out / "state.json"
        self.t0 = f"{dt.datetime.now():%F %T}"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else \
            {"done": [], "manifest": {}, "checklist": {}, "partitions": {}}

    def save(self):
        self.state_path.write_text(json.dumps(self.state, indent=1))

    # ---- core primitive: path list on device -> streamed tar -> verified listing
    def tar(self, name, snippet, force=False, extra=None):
        dst = self.arch / f"{name}.tar"
        have = dst.exists() and name in self.state["manifest"]
        if have and self.args.update and not self.args.redo:
            return self._tar_update(name, snippet, extra)
        if have and not self.args.redo and not force:
            ui.ok(f"{name}.tar already present (--redo to refresh, `update` to add changes)"); return
        d = self.dev
        n = d.sh(f"mkdir -p {DTMP}; cd /; {{ {snippet} ; }} | while IFS= read -r p; do "
                 f"[ -e \"$p\" ] && printf '%s\\n' \"$p\"; done > {DTMP}/{name}.list; wc -l < {DTMP}/{name}.list").strip()
        if n in ("", "0"):
            ui.warn(f"{name}: nothing to back up (no source paths exist)"); return
        prune = " -o ".join(f"-name '{p}'" for p in PRUNE)
        dcount = int(d.sh(f"cd /; while IFS= read -r p; do find \"$p\" \\( {prune} \\) -prune -o -print; "
                          f"done < {DTMP}/{name}.list | wc -l").strip() or 0)
        excl = " ".join(f"--exclude='{p}'" for p in PRUNE)
        nbytes, digest, rc = d.stream(f"cd / && {d.tar} -cf - {excl} -T {DTMP}/{name}.list 2>{DTMP}/{name}.err",
                                      dst, f"{name} ({dcount} entries)")
        err = d.sh(f"cat {DTMP}/{name}.err 2>/dev/null")
        if err.strip():
            (self.arch / f"{name}.errors.txt").write_text(err, encoding="utf-8")
        try:
            names, idx = [], {}
            with tarfile.open(dst, "r|") as t:
                for m in t:
                    names.append(m.name + ("/" if m.isdir() else ""))
                    _index_member(idx, m)
        except (tarfile.TarError, EOFError) as e:
            ui.warn(f"{name}: archive unreadable ({e}) — stream truncated? re-run with --redo"); return
        (self.arch / f"{name}.list").write_text("\n".join(names) + "\n", encoding="utf-8")
        shutil.rmtree(self.arch / f"{name}.d", ignore_errors=True)   # a full copy supersedes old updates
        self._save_index(name, idx)
        self.changed = True
        lcount, socks = len(names), err.count("socket ignored")
        status = "OK"
        if lcount == dcount:
            ui.ok(f"{name}.tar  {human(nbytes)}  {lcount} entries  verified")
        elif dcount - lcount == socks:
            ui.ok(f"{name}.tar  {human(nbytes)}  {lcount} entries  verified (+{socks} runtime sockets, not archivable)")
        else:
            status = f"COUNT-MISMATCH device={dcount} archive={lcount}"
            ui.warn(f"{name}: device has {dcount} entries, archive {lcount} — files changed during copy or "
                    f"were unreadable; see {name}.errors.txt (re-run with --redo once the app is closed)")
        self.state["manifest"][name] = {"bytes": nbytes, "sha256": digest, "device_entries": dcount,
                                        "archive_entries": lcount, "status": status, "rc": rc,
                                        "time": f"{dt.datetime.now():%F %T}", **(extra or {})}
        self.save()

    # ---- incremental updates: base tar + numbered delta tars (+ .deleted lists)
    def _index_path(self, name):
        return self.arch / f"{name}.index.json"

    def _save_index(self, name, idx):
        self._index_path(name).write_text(json.dumps(idx, separators=(",", ":")), encoding="utf-8")

    def _index(self, name):
        """{path: [size, mtime]} of what the backup currently holds (base + all deltas)."""
        p = self._index_path(name)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        ui.say(f"{name}: building file index from the existing archive (one-time)…")
        idx = {}
        for t in archive_chain(self.arch, name):
            if t.suffix == ".tar":
                with tarfile.open(t, "r") as tf:
                    for m in tf:
                        _index_member(idx, m)
            else:
                for line in t.read_text(encoding="utf-8").splitlines():
                    idx.pop(line, None)
        self._save_index(name, idx)
        return idx

    def _tar_update(self, name, snippet, extra):
        d, idx = self.dev, self._index(name)
        n = d.sh(f"mkdir -p {DTMP}; cd /; {{ {snippet} ; }} | while IFS= read -r p; do "
                 f"[ -e \"$p\" ] && printf '%s\\n' \"$p\"; done > {DTMP}/{name}.list; wc -l < {DTMP}/{name}.list").strip()
        prune = " -o ".join(f"-name '{p}'" for p in PRUNE)
        scan = d.sh(f"cd /; while IFS= read -r p; do find \"$p\" \\( {prune} \\) -prune -o \\( -type f -o -type l \\) "
                    f"-exec stat -c '%F|%s|%Y|%n' {{}} +; done < {DTMP}/{name}.list") if n not in ("", "0") else ""
        cur = {}
        for line in scan.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4 and parts[1].isdigit() and parts[2].isdigit():
                cur[parts[3]] = [-1 if parts[0] == "symbolic link" else int(parts[1]), int(parts[2])]
        changed = sorted(p for p, v in cur.items() if idx.get(p) != v)
        deleted = sorted(p for p in idx if p not in cur)
        m = self.state["manifest"][name]
        if extra:
            m.update(extra)
        if not changed and not deleted:
            ui.ok(f"{name}: unchanged"); self.save(); return
        ddir = self.arch / f"{name}.d"; ddir.mkdir(exist_ok=True)
        seq = len({x.stem for x in ddir.iterdir()}) + 1
        stem = ddir / f"{seq:03d}-{dt.datetime.now():%Y%m%d-%H%M%S}"
        delta = {"seq": seq, "time": f"{dt.datetime.now():%F %T}", "changed": len(changed),
                 "deleted": len(deleted), "bytes": 0, "sha256": None}
        if changed:
            lst = Path(str(stem) + ".files")
            lst.write_text("\n".join(changed) + "\n", encoding="utf-8")
            subprocess.run([d.adb, "-s", d.serial, "push", str(lst), f"{DTMP}/{name}.delta"], capture_output=True)
            d.sh(f"chmod 644 {DTMP}/{name}.delta")
            nbytes, digest, _rc = d.stream(f"cd / && {d.tar} -cf - --no-recursion -T {DTMP}/{name}.delta "
                                           f"2>{DTMP}/{name}.err", Path(str(stem) + ".tar"), f"{name} update")
            with tarfile.open(str(stem) + ".tar", "r") as tf:
                got = {mm.name for mm in tf}
            missing = [p for p in changed if p not in got]
            if missing:   # vanished between scan and copy: don't record them as backed up
                for p in missing:
                    cur.pop(p, None)
                ui.warn(f"{name}: {len(missing)} changed file(s) vanished during the update (temp files?)")
            delta.update(bytes=nbytes, sha256=digest, changed=len(changed) - len(missing))
        if deleted:
            Path(str(stem) + ".deleted").write_text("\n".join(deleted) + "\n", encoding="utf-8")
        for p in deleted:
            idx.pop(p, None)
        for p in changed:
            if p in cur:
                idx[p] = cur[p]
        self._save_index(name, idx)
        (self.arch / f"{name}.list").write_text("\n".join(sorted(idx)) + "\n", encoding="utf-8")
        m.setdefault("deltas", []).append(delta)
        m["updated"] = delta["time"]
        self.save(); self.changed = True
        ui.ok(f"{name}: +{delta['changed']} new/changed ({human(delta['bytes'])}), -{len(deleted)} deleted → update #{seq}")

    def prof(self, key, default=None):
        return self.profile.get(key, default if default is not None else [])

    # ================================================================ phases
    def p_preflight(self):
        ui.hdr("preflight")
        d = self.dev
        ui.ok(f"device {d.serial}  {d.sh('getprop ro.product.model', root=False).strip()} "
              f"({d.sh('getprop ro.product.device', root=False).strip()}), build "
              f"{d.sh('getprop ro.build.display.id', root=False).strip()}")
        if d.root:
            ui.ok(f"root via su  ·  tar = {d.tar}  ·  partitions = {d.byname or 'not found'}")
        else:
            ui.warn("NO ROOT: only shared storage + inventory will be backed up (no app data, "
                    "system state, partitions, Android/data)")
        ui.ok(f"users: {d.users}  ·  slot: {d.slot or 'A-only'}")
        bat = d.sh("dumpsys battery", root=False)
        lvl = int((re.search(r"level: (\d+)", bat) or [0, 0])[1])
        charging = bool(re.search(r"status: 2", bat))
        (ui.ok if lvl >= 30 or charging else ui.warn)(f"battery {lvl}%{' (charging)' if charging else ''}")
        d.sh("settings put global stay_on_while_plugged_in 7", root=False)
        ui.ok("screen kept awake while plugged in (reverted in report phase)")
        paths = " ".join(d.media(u) for u in d.users) + (" /data/adb /data/local/tmp" if d.root else "")
        need = sum(int(x.split()[0]) for x in d.sh(f"cd /; du -sm {paths} 2>/dev/null").splitlines() if x.strip())
        free = shutil.disk_usage(self.out).free // (1 << 20)
        ui.say(f"estimated ≥ {need / 1024:.1f} GB needed (+ app data), {free / 1024:.1f} GB free here")
        if free < need + 8192:
            ui.warn("free space is tight (< estimate + 8 GB)")
        d.sh(f"mkdir -p {DTMP}")
        (self.out / "device.json").write_text(json.dumps({
            "serial": d.serial, "root": d.root, "users": d.users, "slot": d.slot,
            "fingerprint": d.sh("getprop ro.build.fingerprint", root=False).strip(),
            "display": d.sh("getprop ro.build.display.id", root=False).strip(),
            "started": f"{dt.datetime.now():%F %T}"}, indent=1))

    def _wa_backups(self):
        """[(label, user, age_seconds or None)] for every WhatsApp / WA Business install, from the real backup file."""
        d, res = self.dev, []
        now = int(d.sh("date +%s", root=False).strip() or time.time())
        for u in d.users:
            pk = d.packages(u)
            for pkg, nm, folder in [("com.whatsapp", "WhatsApp", "WhatsApp"),
                                    ("com.whatsapp.w4b", "WhatsApp Business", "WhatsApp Business")]:
                if pkg not in pk:
                    continue
                base = f"/{d.media(u)}/Android/media/{pkg}/{folder}/Databases"
                ts = d.sh(f"ls -t {base}/msgstore*.crypt1* 2>/dev/null | head -1 | while read f; do stat -c %Y \"$f\"; done").strip()
                res.append((f"{nm} (user {u})", u, now - int(ts) if ts.isdigit() else None))
        return res

    def p_checklist(self):
        ui.hdr("checklist — things only you can do on the phone, BEFORE the wipe")
        d, items = self.dev, []
        cl = self.state["checklist"]
        if d.root:
            for label, _u, age in self._wa_backups():
                if age is not None and age < WA_FRESH:
                    cl[label] = "DONE"
                elif cl.get(label) == "DONE":
                    cl[label] = "OPEN"   # was done, but the backup on the phone is old again
        pk = {u: d.packages(u) for u in d.users}
        for u in d.users:
            for wa, nm in [("com.whatsapp", "WhatsApp"), ("com.whatsapp.w4b", "WhatsApp Business")]:
                if wa in pk[u]:
                    items.append((f"{nm} (user {u})", [
                        f"Open {nm}{' inside your work profile / Island' if u else ''} › Settings › Chats › Chat backup › "
                        "tap Back up. Wait until it finishes.",
                        "If 'End-to-end encrypted backup' is ON, make sure you know its password / 64-digit key.",
                        "(I check the backup file on the phone myself, so I'll notice if it didn't happen.)"]))
        for a in AUTHENTICATORS:
            if any(a in pk[u] for u in d.users):
                items.append((f"Authenticator {a}", [
                    "Your 2FA codes do NOT survive a wipe through file backups (they're locked to this phone's chip).",
                    "In the app: export your codes (or turn on its cloud backup and note the password),",
                    "then copy the export file to this computer."]))
        accts = re.findall(r"Account \{name=[^,]+, type=com\.google\}", d.sh("dumpsys account", root=False))
        if accts:
            items.append((f"Google accounts ({len(accts)})", ["Know every password; have backup codes or another 2FA device.",
                          "Sign-in prompts on THIS phone stop working after the wipe; FRP may ask for an account."]))
        allpk = set().union(*pk.values())
        if "com.google.android.apps.photos" in allpk:
            items.append(("Google Photos", ["Shows 'Backup complete'. If quality = Storage saver, the local files are your only originals."]))
        items.append(("SMS / call log XML export", ["Recommended: SMS Backup & Restore → export to XML, copy to this computer.",
                      "(the tool also copies mmssms.db / calllog.db + text dumps; XML restores most reliably)"]))
        if "com.android.chrome" in allpk:
            items.append(("Chrome", ["Sync ON for passwords/bookmarks, or Settings › Passwords › Export."]))
        if "org.telegram.messenger" in allpk:
            items.append(("Telegram", ["Know the 2-step cloud password for every account."]))
        if "org.thoughtcrime.securesms" in allpk:
            items.append(("Signal", ["Create a backup / enable transfer; note the 30-digit passphrase."]))
        banks = sorted(p for p in allpk if BANK_HINT.search(p)) + self.prof("banking_apps")
        if banks:
            items.append(("Banking / UPI / wallets", [f"{', '.join(sorted(set(banks))[:12])}{' …' if len(set(banks)) > 12 else ''}",
                          "Know MPINs/UPI PINs; keep the bound SIM inserted. Device binding is re-done after the wipe."]))
        if d.root and d.exists("/data/adb/lspd"):
            items.append(("LSPosed", ["Tool saves scopes + module APKs; YOU reinstall the manager and re-scope in its UI."]))
        for it in self.prof("manual_items"):
            items.append((it[0], it[1:]))
        items.append(("New ROM files", ["New ROM zip downloaded + checksum verified; required firmware noted;",
                                        "root build (KernelSU/Magisk) matching the NEW kernel; current ROM zip kept for rollback."]))
        items.append(("Charge", ["Battery ≥ 60% before flashing."]))
        current = {t for t, _l in items}
        for t in [t for t in cl if t not in current and not t.startswith("Before wipe:")]:
            del cl[t]   # item no longer applies (app removed / profile changed)
        done = [t for t, _l in items if cl.get(t) in ("DONE", "N/A")]
        todo = [(t, l) for t, l in items if cl.get(t) not in ("DONE", "N/A")]
        if done:
            ui.ok(f"{len(done)} item(s) already done: " + ", ".join(done))
        if not todo:
            ui.ok("nothing left to do here"); self.save(); return
        print(f"  {len(todo)} thing(s) to do on your phone. For each: do it, then answer.")
        print(f"  Keys: {ui.B}y{ui.N} = done   {ui.B}Enter{ui.N} = not yet (I'll remind you)   {ui.B}x{ui.N} = doesn't apply to me")
        for i, (title, lines) in enumerate(todo, 1):
            print(f"\n  {ui.B}[{i}/{len(todo)}] {title}{ui.N}")
            for l in lines:
                print(f"     {l}")
            a = ui.ask("done? [y / Enter / x]", "UNCONFIRMED").lower()
            if a in ("y", "yes") and title.startswith("WhatsApp") and d.root:
                fresh = {lb: ag for lb, _u, ag in self._wa_backups()}
                if title in fresh and (fresh[title] is None or fresh[title] >= WA_FRESH):
                    ui.warn(f"{title}: the phone still shows an old backup ({ago(fresh[title])}). "
                            "Tap 'Back up' in WhatsApp and wait until it finishes. I'll check again when copying.")
                    a = ""
            cl[title] = {"y": "DONE", "yes": "DONE", "x": "N/A"}.get(a, "OPEN" if a != "unconfirmed" else "UNCONFIRMED")
        self.save()

    def p_inventory(self):
        ui.hdr("inventory — the rebuild checklist")
        d, inv = self.dev, self.out / "inventory"
        inv.mkdir(exist_ok=True)
        cmds = {
            "getprop.txt": "getprop", "users.txt": "pm list users",
            "packages_system.txt": "pm list packages -s", "packages_disabled.txt": "pm list packages -d",
            "accounts.txt": "dumpsys account | grep 'Account {'", "roles_default_apps.txt": "dumpsys role",
            "launcher_ime.txt": "cmd package resolve-activity --brief -c android.intent.category.HOME "
                                "-a android.intent.action.MAIN; settings get secure default_input_method",
            "storage.txt": "df -h",
        }
        for u in d.users:
            cmds[f"packages_user{u}.txt"] = f"pm list packages -3 -i -U --show-versioncode --user {u}"
            for ns in ["global", "secure", "system"]:
                cmds[f"settings_{ns}_user{u}.txt"] = f"settings --user {u} list {ns}"
        if d.root:
            cmds.update({
                "root_modules.txt": "for f in /data/adb/modules/*/module.prop; do echo \"== $f\"; cat \"$f\"; "
                                    "[ -e \"${f%/*}/disable\" ] && echo STATE=DISABLED; done",
                "tricky_store.txt": "ls -la /data/adb/tricky_store 2>/dev/null && cat /data/adb/tricky_store/target.txt "
                                    "/data/adb/tricky_store/security_patch.txt 2>/dev/null",
                "partitions.txt": f"ls -la {d.byname or '/dev/block'}",
                "data_sizes.txt": "du -sm /data/data/* 2>/dev/null | sort -rn",
            })
        for fn, c in cmds.items():
            (inv / fn).write_text(d.sh(c), encoding="utf-8")
        if d.root and d.exists("/data/adb/lspd/config/modules_config.db"):
            for ext in ["", "-wal", "-shm"]:
                (inv / f"lspd_modules_config.db{ext}").write_bytes(
                    d.sh(f"cat /data/adb/lspd/config/modules_config.db{ext} 2>/dev/null", binary=True))
            try:
                con = sqlite3.connect(f"file:{inv / 'lspd_modules_config.db'}?mode=ro", uri=True)
                rows = con.execute(
                    "SELECT m.module_pkg_name, s.user_id, COALESCE(st.enabled,0), s.app_pkg_name FROM modules m "
                    "LEFT JOIN scope s ON s.module_pkg_name=m.module_pkg_name LEFT JOIN modules_state st "
                    "ON st.module_pkg_name=m.module_pkg_name AND st.user_id=COALESCE(s.user_id,0) "
                    "ORDER BY 1,2,4").fetchall()
                lines = [f"{'module':38} {'user':>4} {'on':>2}  scoped_app"] + \
                        [f"{m:38} {'' if u is None else u:>4} {e:>2}  {a or ''}" for m, u, e, a in rows]
                (inv / "lspd_scopes.txt").write_text("\n".join(lines) + "\n")
                ui.ok(f"LSPosed scopes: {len(rows)} rows → inventory/lspd_scopes.txt (re-apply in the Manager UI)")
            except sqlite3.Error as e:
                ui.warn(f"LSPosed DB unreadable ({e}); raw copy kept")
        ui.ok(f"inventory/ written ({len(list(inv.iterdir()))} files)")

    def p_partitions(self):
        ui.hdr("partitions — per-handset identity/calibration + current boot chain (sha256-verified)")
        d = self.dev
        if not (d.root and d.byname):
            ui.warn("skipped (needs root + by-name partition dir)"); return
        pdir = self.out / "partitions"; pdir.mkdir(exist_ok=True)
        present = set(d.sh(f"ls {d.byname}").split())
        wanted = [p for p in NV_PARTS if p in present] + self.prof("extra_partitions")
        wanted += [p + d.slot for p in BOOT_PARTS if p + d.slot in present]
        if self.args.super and "super" in present:
            wanted.append("super")
        for p in wanted:
            f = pdir / f"{p}.img"
            if f.exists() and p in self.state["partitions"] and not self.args.redo:
                if not self.args.update:
                    ui.ok(f"{p} (cached)"); continue
                now = d.sh(f"sha256sum {d.byname}/{p}").split()[:1]
                if now == [self.state["partitions"][p]["sha256"]] or not self.state["partitions"][p]["exact"]:
                    ui.ok(f"{p}: unchanged" + ("" if self.state["partitions"][p]["exact"] else " (live partition; file copy refreshed below)"))
                    continue
            size = int(d.sh(f"blockdev --getsize64 {d.byname}/{p}").strip() or 0)
            if size > BIG_PART_LIMIT and p != "super":
                ui.warn(f"{p} is {human(size)}, skipped (raise via profile extra_partitions if needed)"); continue
            exact = False
            for _ in range(3):  # live-mounted partitions (e.g. MTK nvdata) can change mid-read
                n, local, _rc = d.stream(f"dd if={d.byname}/{p} bs=4M 2>/dev/null", f, p)
                dev_sum = d.sh(f"sha256sum {d.byname}/{p}").split()[:1]
                if dev_sum and dev_sum[0] == local:
                    exact = True; break
            self.state["partitions"][p] = {"bytes": n, "sha256": local, "exact": exact}
            self.changed = True
            (ui.ok if exact else ui.warn)(f"{p}.img  {human(n)}  " + ("sha256 match" if exact else
                                          "changes while mounted — kept, not bit-exact (file copy below)"))
            self.save()
        mnt = d.sh("ls -d /mnt/vendor/* /efs /mnt/vendor/efs 2>/dev/null").split()
        if mnt:
            self.tar("nv-mounted-files", "printf '%s\\n' " + " ".join(m.lstrip("/") for m in mnt))
        (pdir / "README.txt").write_text(
            f"Slot at backup time: {d.slot or 'A-only'}.\nNV/persist/proinfo/protect*/modemst*/fsg/efs hold IMEI and RF "
            "calibration. Restore ONLY onto this same handset and ONLY if IMEI/baseband breaks. A ROM clean flash "
            "does not touch them.\n")

    def p_system(self):
        ui.hdr("root + system state")
        if not self.dev.root:
            ui.warn("skipped (no root)"); return
        self.tar("root-adb", "ls -d data/adb 2>/dev/null")
        self.tar("local-tmp", f"ls -A data/local/tmp | grep -v -x '{Path(DTMP).name}' | sed 's|^|data/local/tmp/|'")
        users = self.dev.users
        per_user = " ".join(f"data/system/users/{u} data/misc/user/{u}" for u in users)
        providers = []
        for prov in ["com.android.providers.telephony", "com.android.providers.contacts",
                     "com.android.providers.blockednumber", "com.android.providers.calendar",
                     "com.android.providers.userdictionary", "com.android.deskclock", "com.google.android.deskclock",
                     "com.android.launcher3", "com.google.android.apps.nexuslauncher"]:
            providers += [f"data/data/{prov}", f"data/user_de/0/{prov}"]
        self.tar("system-state", "printf '%s\\n' data/misc/adb data/misc/bluedroid data/misc/bluetooth "
                 "data/misc/apexdata/com.android.wifi data/misc/wifi data/misc/vpn data/system/sync "
                 "data/system/users/userlist.xml data/system/packages.xml data/system/appops.xml "
                 f"{per_user} {' '.join(providers)}")

    def p_exports(self):
        ui.hdr("human-readable exports (fallback if DB restore fails across Android versions)")
        d, ex = self.dev, self.out / "exports"
        ex.mkdir(exist_ok=True)
        q = {"sms": "content://sms", "mms_index": "content://mms", "call_log": "content://call_log/calls",
             "blocked_numbers": "content://com.android.blockednumber/blocked"}
        for fn, uri in q.items():
            (ex / f"{fn}.txt").write_text(d.sh(f"content query --uri {uri}"), encoding="utf-8")
        (ex / "contacts_flat.txt").write_text(d.sh("content query --uri content://com.android.contacts/data "
                                                   "--projection display_name:data1:mimetype"), encoding="utf-8")
        for fn in ["sms", "call_log", "contacts_flat"]:
            ui.ok(f"{fn}: {(ex / f'{fn}.txt').read_text(encoding='utf-8').count('Row:')} rows")
        if d.root:
            wifi = d.sh("cat /data/misc/apexdata/com.android.wifi/WifiConfigStore.xml 2>/dev/null || "
                        "cat /data/misc/wifi/WifiConfigStore.xml 2>/dev/null")
            nets = re.findall(r'name="SSID">([^<]*)<.*?(?:name="PreSharedKey">([^<]*)<|</Network>)', wifi, re.S)
            (ex / "wifi_networks.txt").write_text("\n".join(f"{s}\t{k}" for s, k in nets) + "\n", encoding="utf-8")
            ui.ok(f"wifi networks: {len(nets)} (SSID + key in exports/wifi_networks.txt)")

    def _messengers_fresh(self):
        """Gate before copying messenger data: were the in-app backups confirmed in the checklist?
        Returns True if fresh. If not, asks to stop (interactive) or warns (--yes)."""
        if hasattr(self, "_fresh"):
            return self._fresh
        users = [u for u in self.dev.users
                 if {"com.whatsapp", "com.whatsapp.w4b"} & set(self.dev.packages(u))]
        if not users:
            self._fresh = True; return True
        cl = self.state["checklist"]
        if not self.dev.root:   # can't read the backup files without root: fall back to the checklist answer
            ok_ = all(cl.get(t) in ("DONE", "N/A") for t in cl if t.startswith("WhatsApp")) and \
                  any(t.startswith("WhatsApp") for t in cl)
            self._fresh = ok_; return ok_
        while True:
            backups = self._wa_backups()
            old = [(lb, ag) for lb, _u, ag in backups if ag is None or ag >= WA_FRESH]
            for lb, _u, ag in backups:
                if ag is not None and ag < WA_FRESH:
                    cl[lb] = "DONE"
            self.save()
            if not old:
                ui.ok("WhatsApp backups are fresh: " + ", ".join(f"{lb} {ago(ag)}" for lb, _u, ag in backups))
                self._fresh = True; return True
            ui.warn("WhatsApp backup is old, so recent chats would be missing: "
                    + ", ".join(f"{lb} last backed up {ago(ag)}" for lb, ag in old))
            if ui.yes:
                self._fresh = False; return False
            names = [re.sub(r" \(user 0\)$", "", lb) if lb.endswith("(user 0)") else
                     re.sub(r" \(user \d+\)$", " inside your work profile (Island)", lb) for lb, _ in old]
            print("     On the phone, open " + " and ".join(names)
                  + " › Settings › Chats › Chat backup › Back up. Wait for it to finish.")
            a = ui.ask(f"{ui.B}Enter{ui.N} = I did it, check again  ·  {ui.B}s{ui.N} = skip, use the older backup  ·  "
                       f"{ui.B}q{ui.N} = quit for now:", "s").lower()
            if a == "s":
                self._fresh = False; return False
            if a == "q":
                ui.say("stopped. Run the same command again any time — it continues where it left off.")
                sys.exit(0)

    def _messenger_tar(self, name, snippet):
        fresh = self._messengers_fresh()
        prev = self.state["manifest"].get(name, {})
        stale_before = prev and not prev.get("messengers_fresh", False)
        if fresh and stale_before:
            ui.say(f"{name}: previous copy was made before the WhatsApp backup was confirmed — refreshing")
        self.tar(name, snippet, force=bool(fresh and stale_before), extra={"messengers_fresh": fresh})

    def p_storage(self):
        ui.hdr("internal shared storage (user 0) — split for resumability")
        M = self.dev.media(0)
        self._messenger_tar("sd-messengers", f"printf '%s\\n' {M}/Android/media/com.whatsapp {M}/Android/media/com.whatsapp.w4b "
                            f"{M}/WhatsApp {M}/Telegram {M}/Signal")
        self.tar("sd-camera", f"printf '%s\\n' {M}/DCIM {M}/Pictures {M}/Movies")
        self.tar("sd-download", f"echo {M}/Download")
        if self.dev.root:
            self.tar("sd-android-data", f"echo {M}/Android/data")
        else:
            ui.warn("Android/data not readable without root (Android 11+): app-private media, call-recorder "
                    "folders etc. are NOT backed up")
        top = "Android|DCIM|Pictures|Movies|Download|WhatsApp|Telegram|Signal|\\.transforms|\\.picker_transcoded"
        self.tar("sd-rest", f"ls -A {M} | grep -v -x -E '{top}' | sed 's|^|{M}/|'; "
                 f"ls -A {M}/Android/media 2>/dev/null | grep -v -x -E 'com\\.whatsapp|com\\.whatsapp\\.w4b' "
                 f"| sed 's|^|{M}/Android/media/|'; echo {M}/Android/obb")

    def p_profiles(self):
        ui.hdr("secondary users / work profiles (Island, Shelter, work profile, …)")
        d = self.dev
        others = [u for u in d.users if u != 0]
        if not others:
            ui.ok("none"); return
        if not d.root:
            ui.warn(f"users {others} need root to back up — skipped"); return
        for u in others:
            self._messenger_tar(f"user{u}-media", f"echo data/media/{u}")
            self.tar(f"user{u}-apps", f"ls -A data/user/{u} | grep -v -x com.android.vending | sed 's|^|data/user/{u}/|'; "
                     f"ls -A data/user_de/{u} | grep -v -x com.android.vending | sed 's|^|data/user_de/{u}/|'")

    def p_apps(self):
        ui.hdr("app data + APKs")
        d = self.dev
        if not d.root:
            ui.warn("app data needs root — saving APKs only")
        pk0 = d.packages(0)
        if d.root:
            if self.args.all_apps:
                apps = list(pk0)
            else:
                apps = [a for a in self.prof("curated_apps") if a in pk0 or self._is_pkg(a)]
                apps += [a for a in self.state.get("apps_selected", []) if a in pk0 and a not in apps]
                if not ui.yes and (self.args.pick_apps or "apps_selected" not in self.state):
                    sizes = {}
                    for l in d.sh("du -sm /data/data/* 2>/dev/null | sort -rn").splitlines():
                        m = re.match(r"(\d+)\s+/data/data/(\S+)", l)
                        if m and m.group(2) in pk0:
                            sizes[m.group(2)] = int(m.group(1))
                    print(f"  Selected: {len(apps)} app(s) from profile. User apps by data size (MB):")
                    rows = sorted(sizes.items(), key=lambda x: -x[1])
                    for i, (p, s) in enumerate(rows, 1):
                        tag = "*" if p in apps else ("R" if p in KEYSTORE_BOUND else " ")
                        print(f"   {tag}{i:3}  {s:6}  {p}")
                    print("   (* selected, R = keystore-bound: always kept as reference only)")
                    extra = ui.ask("add apps (numbers or package names, space-separated; Enter = none):")
                    for tok in extra.split():
                        if tok.isdigit() and 0 < int(tok) <= len(rows):
                            apps.append(rows[int(tok) - 1][0])
                        elif tok:
                            apps.append(tok)
            self.state["apps_selected"] = sorted(set(apps)); self.save()
            ref = [a for a in set(KEYSTORE_BOUND + self.prof("reference_apps")) if a in pk0 or self._is_pkg(a)]
            for pkg in sorted(set(apps) | set(ref)):
                self.tar(f"app-{pkg}", f"printf '%s\\n' data/data/{pkg} data/user_de/0/{pkg}")
            kd = self.out / "messenger-keys"; kd.mkdir(exist_ok=True)
            for u in d.users:
                for wa in ["com.whatsapp", "com.whatsapp.w4b"]:
                    base = f"/data/data/{wa}/files" if u == 0 else f"/data/user/{u}/{wa}/files"
                    for k in ["key", "encrypted_backup.key"]:
                        if d.exists(f"{base}/{k}"):
                            (kd / f"user{u}_{wa}_{k}").write_bytes(d.sh(f"cat {base}/{k}", binary=True))
                            ui.ok(f"{wa} user {u}: {k} saved (decrypts local crypt14/15 with wa-crypt-tools)")
        apkdir = self.out / "apks"; apkdir.mkdir(exist_ok=True)
        if self.args.all_apks:
            want = set(pk0)
        else:
            want = {p for p, inst in pk0.items() if inst != "com.android.vending"} | set(self.prof("always_apks"))
        for pkg in sorted(want):
            paths = [l.replace("package:", "").strip() for l in d.sh(f"pm path {pkg}", root=False).splitlines() if l.strip()]
            if not paths:
                ui.warn(f"apk {pkg}: not installed"); continue
            (apkdir / pkg).mkdir(exist_ok=True)
            names = {Path(ap).name for ap in paths}
            for old in (apkdir / pkg).glob("*.apk"):
                if old.name not in names:      # app updated: split names changed
                    old.unlink()
            good = 0
            for ap in paths:
                lf = apkdir / pkg / Path(ap).name
                meta = d.sh(f"stat -c '%s %Y' '{ap}'").strip()
                if lf.exists() and not self.args.redo:
                    if not self.args.update or self.state.setdefault("apk_meta", {}).get(ap) == meta:
                        self.state.setdefault("apk_meta", {})[ap] = meta
                        good += 1; continue
                    self.changed = True
                _n, local, _rc = d.stream(f"cat '{ap}'", lf, f"apk {pkg}")
                if d.sh(f"sha256sum '{ap}'").split()[:1] == [local]:
                    good += 1; self.state.setdefault("apk_meta", {})[ap] = meta
                else:
                    ui.warn(f"apk {pkg}/{lf.name}: checksum mismatch")
            ui.ok(f"apk {pkg}  {good}/{len(paths)} file(s)")
        self.save()

    def _is_pkg(self, p):
        return bool(self.dev.sh(f"pm path {p}", root=False).strip())

    def p_verify(self):
        ui.hdr("verification")
        d, fail = self.dev, False
        listed = []
        for lf in sorted(self.arch.glob("*.list")):
            listed += lf.read_text(encoding="utf-8").splitlines()
        blob = "\n".join(listed)
        # 1. critical items: present on device => must be in an archive
        checks = []
        for label, rx in CRITICAL + [tuple(x) for x in self.prof("must_have")]:
            if "{u}" in rx:
                checks += [(label.replace("{u}", str(u)), rx.replace("{u}", str(u))) for u in d.users]
            else:
                checks.append((label, rx))
        dev_index = self._device_index()
        for label, rx in checks:
            r = re.compile(rx, re.M)
            on_dev, in_arch = bool(r.search(dev_index)), bool(r.search(blob))
            if in_arch:
                ui.ok(label)
            elif on_dev:
                ui.warn(f"MISSING: {label} — exists on device but in no archive"); fail = True
        # 2. archive integrity (re-read every byte) + count status
        quick = self.args.update and not self.args.deep
        for name, m in self.state["manifest"].items():
            t = self.arch / f"{name}.tar"
            if not t.exists() or t.stat().st_size != m["bytes"] or (not quick and sha256_file(t) != m["sha256"]):
                ui.warn(f"CORRUPT/CHANGED on disk: {name}.tar"); fail = True
            elif m["status"] != "OK":
                ui.warn(f"{name}: {m['status']}")
        for name, m in self.state["manifest"].items():
            for dl in m.get("deltas", []):
                hits = sorted((self.arch / f"{name}.d").glob(f"{dl['seq']:03d}-*.tar"))
                if quick and hits and dl["time"] < self.t0:
                    continue   # older update, already verified when written
                if dl.get("sha256") and (not hits or sha256_file(hits[0]) != dl["sha256"]):
                    ui.warn(f"CORRUPT/CHANGED on disk: {name} update #{dl['seq']}"); fail = True
        ui.ok(f"{len(self.state['manifest'])} archives " + ("size-checked, this run's updates re-hashed (--deep: re-hash all)"
                                                          if quick else "re-hashed (incl. updates)"))
        for p, m in self.state["partitions"].items():
            f = self.out / "partitions" / f"{p}.img"
            if not f.exists() or f.stat().st_size != m["bytes"] or (not quick and sha256_file(f) != m["sha256"]):
                ui.warn(f"partition image {p} corrupt/missing"); fail = True
        # 3. what is deliberately NOT backed up (so it's a decision, not an accident)
        if d.root:
            skipped = []
            for l in d.sh("du -sm /data/data/* 2>/dev/null | sort -rn").splitlines():
                m = re.match(r"(\d+)\s+/data/data/(\S+)", l)
                if m and int(m.group(1)) >= 20 and f"data/data/{m.group(2)}/" not in blob:
                    skipped.append(f"{int(m.group(1)):6} MB  {m.group(2)}")
            (self.out / "NOT_BACKED_UP.txt").write_text(
                "App data intentionally not backed up (cloud-synced / re-login / caches). Review this list:\n"
                + "\n".join(skipped) + "\n")
            ui.say(f"{len(skipped)} apps with ≥20 MB data NOT backed up → NOT_BACKED_UP.txt (review it)")
        self.state["verify_ok"] = not fail; self.save()
        ui.say(f"{ui.G}verification PASSED{ui.N}" if not fail else f"{ui.R}verification FAILED — see WARNINGS.txt{ui.N}")

    def _device_index(self):
        """Compact listing of critical locations on the device to test CRITICAL regexes against."""
        d = self.dev
        roots = ["data/adb/tricky_store", "data/adb/lspd/config", "data/misc/adb", "data/misc/bluedroid",
                 "data/misc/apexdata/com.android.wifi", "data/misc/wifi", "data/user_de/0", "data/data"]
        cmd = "cd /; "
        if d.root:
            cmd += "ls -d data/adb/pif.* data/adb/modules/playintegrityfix/*pif* data/adb/modules/*/ 2>/dev/null; "
            cmd += "for r in " + " ".join(roots[:6]) + "; do ls -d $r/* 2>/dev/null; done; "
            cmd += "ls -d data/user_de/0/*/databases/* data/data/*/databases/* 2>/dev/null | grep -E 'mmssms|calllog|alarms'; "
        for u in d.users:
            M = d.media(u)
            cmd += (f"ls -d {M}/Android/media/com.whatsapp*/*/Databases/* {M}/DCIM/Camera/* {M}/Pictures/Screenshots/* "
                    f"{M}/DCIM/Screenshots/* {M}/Documents/* {M}/Recordings/* {M}/Download/* {M}/* 2>/dev/null | head -400; ")
        out = d.sh(cmd)
        if not d.root:
            out = out.replace("storage/emulated/", "data/media/")
        return out

    def _second_copy(self):
        """Mirror the backup to a remembered folder (e.g. an encrypted external drive). Asked once, then automatic."""
        st, key = self.state, "Before wipe: second, encrypted copy"
        dest = st.get("second_copy")
        if not dest and not ui.yes and st["checklist"].get(key) not in ("DONE", "N/A"):
            print(f"\n  {ui.B}Second copy{ui.N}: plug in an encrypted drive (or any folder on one) and drag it into this window,")
            print("  then press Enter. I'll keep it in sync on every run from now on. Just Enter = skip for now.")
            a = ui.ask("folder:", "").strip().strip("'\"").replace("\\ ", " ")
            if a and not Path(a).expanduser().is_dir():
                ui.warn(f"{a} isn't a folder I can see — skipping the second copy for now"); a = ""
            if a and sys.platform == "darwin":
                info = subprocess.run(["diskutil", "info", str(Path(a).expanduser())], capture_output=True, text=True).stdout
                if re.search(r"(FileVault|Encrypted):\s+No", info) and not re.search(r"(FileVault|Encrypted):\s+Yes", info):
                    ui.warn("that drive is NOT encrypted — anyone holding it could read your passwords and keys")
                    if ui.ask("use it anyway? [y/N]", "n").lower() not in ("y", "yes"):
                        a = ""
            if a:
                st["second_copy"] = dest = str(Path(a).expanduser())
                self.save()
        if not dest:
            return
        target = Path(dest) / self.out.name
        if not Path(dest).exists():
            ui.warn(f"second copy: {dest} isn't connected right now — plug it in and run again")
            st["checklist"][key] = "OPEN"; return
        ui.say(f"syncing second copy → {target}")
        n, nb = mirror(self.out, target)
        st["checklist"][key] = "DONE"; self.save()
        ui.ok(f"second copy up to date ({n} file(s), {human(nb)} copied this time)")

    def p_report(self):
        ui.hdr("report")
        st, out = self.state, self.out
        dev = json.loads((out / "device.json").read_text())
        total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
        L = [f"# Pre-wipe backup — {dt.datetime.now():%F %T}", "",
             f"Device `{dev['serial']}` · `{dev['display']}` · root: {dev['root']} · users: {dev['users']} · "
             f"total {human(total)}", "",
             f"**Verification: {'PASSED' if st.get('verify_ok') else 'FAILED / not run — see WARNINGS.txt'}**", "",
             "## Archives", "", "| archive | size | device entries | archive entries | status |", "|---|---|---|---|---|"]
        for n, m in sorted(st["manifest"].items()):
            ups = m.get("deltas", [])
            upd = f" + {len(ups)} update(s), last {m['updated']}" if ups else ""
            L.append(f"| {n} | {human(m['bytes'] + sum(x['bytes'] for x in ups))}{upd} | {m['device_entries']} | "
                     f"{m['archive_entries']} | {m['status']} |")
        if st["partitions"]:
            L += ["", "## Partition images", ""] + [f"- {p}: {human(m['bytes'])} {'exact' if m['exact'] else 'LIVE (not bit-exact)'}"
                                                  for p, m in st["partitions"].items()]
        nbu = out / "NOT_BACKED_UP.txt"
        skipped = max(0, len(nbu.read_text().splitlines()) - 1) if nbu.exists() else 0
        final = [("Before wipe: reviewed NOT_BACKED_UP.txt",
                  [f"{skipped} apps with ≥20 MB of data were left out on purpose (they come back by logging in).",
                   f"Have a quick look: {nbu}",
                   "Want one included? Run this tool again with --pick-apps and type its number."])]
        self._second_copy()
        if st["checklist"].get("Before wipe: second, encrypted copy") not in ("DONE", "N/A"):
            final.append(("Before wipe: second, encrypted copy",
                          [f"Keep a second copy of {out} on an encrypted drive (it holds passwords and keys).",
                           "Easiest: run this tool again and give it the drive when it asks — it then syncs automatically."]))
        st["checklist"].pop("Before wipe: fresh WhatsApp copy", None)   # covered by the per-WhatsApp items
        final = [f for f in final if st["checklist"].get(f[0]) not in ("DONE", "N/A")]
        if final:
            ui.say("before you wipe — the last steps, which need the finished backup:")
        for title, lines in final:
            print(f"\n  {ui.B}{title}{ui.N}")
            for l in lines:
                print(f"     {l}")
            a = ui.ask("done? [y/N]", "UNCONFIRMED").lower()
            st["checklist"][title] = "DONE" if a in ("y", "yes") else ("UNCONFIRMED" if a == "unconfirmed" else "OPEN")
        self.save()
        opened = [t for t, a in st["checklist"].items() if a not in ("DONE", "N/A")]
        if not any(not t.startswith("Before wipe:") for t in st["checklist"]):
            opened.insert(0, "Answer the phone checklist (it runs automatically next time)")
        safe = bool(st.get("verify_ok")) and not opened
        L[6:6] = [f"## {'✅ SAFE TO WIPE' if safe else '⛔ NOT YET SAFE TO WIPE'}", ""] + \
                 ([] if st.get("verify_ok") else ["- verification did not pass (see Warnings)"]) + \
                 [f"- open: {t}" for t in opened] + [""]
        L += ["", "## Checklist", ""]
        for t, a in st["checklist"].items():
            L.append(f"- [{'x' if a in ('DONE', 'N/A') else ' '}] {t} — {a if a in ('DONE', 'N/A') else '**' + a + '**'}")
        w = out / "WARNINGS.txt"
        L += ["", "## Warnings", ""] + ([f"- {x}" for x in w.read_text().splitlines()] if w.exists() and w.read_text().strip() else ["none"])
        (out / "SUMMARY.md").write_text("\n".join(L) + "\n", encoding="utf-8")
        (out / "RESTORE.md").write_text(RESTORE_GUIDE + "".join(f"\n- {n}" for n in self.prof("restore_notes")) + "\n", encoding="utf-8")
        shutil.copy2(__file__, out / "droidvault.py")
        if self.args.profile:
            shutil.copy2(self.args.profile, out / "profile.json")
        self.dev.sh(f"rm -rf {DTMP}")
        self.dev.sh("settings put global stay_on_while_plugged_in 0", root=False)
        ui.say(f"SUMMARY.md, RESTORE.md, NOT_BACKED_UP.txt in {out}")
        print()
        if safe:
            ui.say(f"{ui.G}{ui.B}✅ SAFE TO WIPE.{ui.N} Everything is backed up, checked, and copied twice.")
            print(f"     After flashing, open {out / 'RESTORE.md'} and follow it top to bottom.")
            print("     Changed something on the phone before flashing? Just run the same command again (takes ~1–2 min).")
        else:
            ui.say(f"{ui.R}{ui.B}⛔ NOT YET SAFE TO WIPE.{ui.N} Still to do:")
            if not st.get("verify_ok"):
                print("     - the backup check found a problem (see WARNINGS.txt)")
            for t in opened:
                print(f"     - {t.replace('Before wipe: ', '')}")
            print(f"     Do those, then run the {ui.B}same command again{ui.N}. It only asks what's still open.")


WA_FRESH = 3 * 3600   # a WhatsApp backup younger than this counts as "fresh"


def ago(sec):
    if sec is None:
        return "never"
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if sec >= n:
            v = sec // n
            return f"{v} {unit}{'s' if v != 1 else ''} ago"
    return "just now"


def mirror(src, dst):
    """Copy new/changed files (size or mtime differ) from src to dst and drop files gone from src."""
    n = nb = 0
    dst.mkdir(parents=True, exist_ok=True)
    seen = set()
    for f in src.rglob("*"):
        rel = f.relative_to(src); seen.add(rel)
        t = dst / rel
        if f.is_dir():
            t.mkdir(parents=True, exist_ok=True); continue
        st_ = f.stat()
        if t.exists() and t.stat().st_size == st_.st_size and int(t.stat().st_mtime) == int(st_.st_mtime):
            continue
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t); n += 1; nb += st_.st_size
        ui.progress("second copy", nb)
    ui.clear()
    for t in sorted(dst.rglob("*"), reverse=True):
        if t.relative_to(dst) not in seen:
            shutil.rmtree(t) if t.is_dir() else t.unlink()
    return n, nb


def _index_member(idx, m):
    if m.isfile():
        idx[m.name] = [m.size, int(m.mtime)]
    elif m.issym():
        idx[m.name] = [-1, int(m.mtime)]


def _owner_writable(root):
    """Extracted Android files keep restrictive modes (e.g. 0500 dirs): make them readable/deletable by you."""
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                try:
                    os.chmod(p, os.stat(p).st_mode | (0o700 if n in dirnames else 0o600))
                except OSError:
                    pass


def rmtree(p):
    """rmtree that also removes read-only leftovers."""
    def fix(func, path, _exc):
        try:
            os.chmod(os.path.dirname(path), 0o700); os.chmod(path, 0o700); func(path)
        except OSError:
            pass
    if Path(p).exists():
        shutil.rmtree(p, onerror=fix)


def archive_chain(arch, name):
    """Base archive, then each update's .tar / .deleted in order — the order to apply on restore."""
    chain = [arch / f"{name}.tar"]
    ddir = arch / f"{name}.d"
    if ddir.is_dir():
        for stem in sorted({x.stem for x in ddir.iterdir() if x.suffix in (".tar", ".deleted")}):
            chain += [x for x in (ddir / f"{stem}.tar", ddir / f"{stem}.deleted") if x.exists()]
    return chain


RESTORE_GUIDE = """# Restore after the wipe

Restore *pieces*, never whole trees: app UIDs change on reinstall and the hardware keystore is new,
so anything keystore-bound (messengers, authenticators, banking) must come back via its own backup/login.

1. **Flash, first boot.** Sign in to Google. Skip Google's "restore apps", or allow it. Then re-root with a
   build matching the NEW kernel. Old boot/init_boot images in `partitions/` are only for rolling back.
2. **ADB trust:** push `data/misc/adb/adb_keys` (from system-state.tar) to `/data/misc/adb/adb_keys`, then
   `chown system:shell`, `chmod 640`, reboot.
3. **Root modules:** reinstall from fresh zips (`inventory/root_modules.txt`). Then copy individual config files
   from `root-adb.tar` (e.g. `tricky_store/keybox.xml`, `target.txt`, `pif.*`). Don't extract the whole tree.
4. **LSPosed:** install the manager, enable modules, and re-scope them **in the UI** from `inventory/lspd_scopes.txt`.
   Never copy `modules_config.db` back.
5. **WiFi:** turn WiFi off. Copy `WifiConfigStore.xml` back (same dir it came from), `chown system:system`,
   `chmod 600`, `restorecon`, reboot. Fallback: `exports/wifi_networks.txt`.
6. **Bluetooth:** with BT off, copy `bt_config.conf` to `/data/misc/bluedroid/`, `chown bluetooth:bluetooth`, `chmod 660`, reboot.
7. **SMS / calls:** restore the XML export (SMS Backup & Restore). Copying `mmssms.db`/`calllog.db` back is a last
   resort, same Android version only. `exports/` has plain-text copies.
8. **WhatsApp (each account, each user):** install, **don't open**. Extract `sd-messengers.tar` (or `userN-media.tar`)
   back into `/sdcard/Android/media/`. Open WhatsApp, verify the number, and pick Drive or local restore. If the local
   backup isn't detected, try with networking off. `messenger-keys/` + wa-crypt-tools decrypt the backup offline.
9. **Media:** extract `sd-camera`, `sd-download`, `sd-rest` and the parts of `sd-android-data` you want
   (e.g. call recordings) back to `/sdcard`. Then reboot or `cmd media_provider rescan`.
10. **App data:** `python droidvault.py restore-app <backup-dir> <pkg> [--user N]`. It installs the saved APK if
    needed, then extracts, re-owns to the new UID, and runs restorecon. Refuses keystore-bound apps.
11. **Work profile / Island:** recreate it, install the apps, and use restore-app with `--user N`.
12. **Partitions (NV/persist/EFS):** only if IMEI/baseband breaks, and only on this same handset.

Device-specific notes:
"""


# ---------------------------------------------------------------------- restore-app
def restore_app(dev, bdir, pkg, user):
    dev.detect()
    ok_, msg = restore_app_data(dev, bdir, pkg, user)
    (ui.ok if ok_ else ui.die)(msg)


def restore_app_data(dev, bdir, pkg, user, dry=False):
    """Install (from saved APKs if needed) and restore one app's data. Returns (ok, message)."""
    t = bdir / "archives" / f"app-{pkg}.tar"
    if not t.exists():
        return False, f"no backup of {pkg}"
    if pkg in KEYSTORE_BOUND:
        return False, f"{pkg} is locked to the old phone's keystore — restore it through the app itself"
    if not dev.root:
        return False, "needs root"
    if not dev.sh(f"pm path --user {user} {pkg}", root=False).strip():
        apks = sorted((bdir / "apks" / pkg).glob("*.apk"))
        if not apks:
            return False, f"{pkg} isn't installed — install it from the Play Store (don't open it), then run again"
        if dry:
            print(f"     [dry-run] adb install-multiple --user {user} {' '.join(a.name for a in apks)}")
        else:
            r = subprocess.run([dev.adb, "-s", dev.serial, "install-multiple", "--user", str(user)] + [str(a) for a in apks],
                               capture_output=True, text=True)
            if r.returncode:
                return False, f"installing {pkg} failed: {r.stdout.strip()[-200:]} {r.stderr.strip()[-200:]}"
    if dry:
        parts = archive_chain(bdir / "archives", f"app-{pkg}")
        print(f"     [dry-run] force-stop {pkg}; apply {len(parts)} archive part(s); chown to new uid; restorecon")
        return True, f"{pkg}: would be restored"
    dev.sh(f"mkdir -p {DTMP}; am force-stop --user {user} {pkg}; rm -rf {DTMP}/x {DTMP}/r*; mkdir -p {DTMP}/x")
    steps = []
    for i, part in enumerate(archive_chain(bdir / "archives", f"app-{pkg}")):
        subprocess.run([dev.adb, "-s", dev.serial, "push", str(part), f"{DTMP}/r{i}"], capture_output=True)
        steps.append(f"{dev.tar} -xf {DTMP}/r{i} -C {DTMP}/x" if part.suffix == ".tar" else
                     f"while IFS= read -r f; do rm -f \"{DTMP}/x/$f\"; done < {DTMP}/r{i}")
    ce = f"/data/data/{pkg}" if user == 0 else f"/data/user/{user}/{pkg}"
    de = f"/data/user_de/{user}/{pkg}"
    res = dev.sh(f"""set -e; uid=$(stat -c %u {ce}); gid=$(stat -c %g {ce})
{chr(10).join(steps)}
[ -d {DTMP}/x/data/data/{pkg} ] && cp -a {DTMP}/x/data/data/{pkg}/. {ce}/
[ -d {DTMP}/x/data/user_de/0/{pkg} ] && cp -a {DTMP}/x/data/user_de/0/{pkg}/. {de}/
chown -R $uid:$gid {ce} {de}; restorecon -RF {ce} {de}; rm -rf {DTMP}/x {DTMP}/r*; echo RESTORED uid=$uid""")
    if "RESTORED" in res:
        return True, f"{pkg} restored ({res.strip()})"
    return False, f"restoring {pkg} failed: {res.strip()[-300:]}"


def extract(bdir, name, dest, quiet=False):
    """Materialise the latest state of an archive (base + updates, deletions applied) into dest."""
    arch = bdir / "archives"
    if not (arch / f"{name}.tar").exists():
        ui.die(f"no archive {name} in {arch} (see SUMMARY.md for names)")
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    for part in archive_chain(arch, name):
        if part.suffix == ".tar":
            with tarfile.open(part, "r") as tf:
                safe = [m for m in tf if os.path.commonpath([str(root), str((root / m.name).resolve())]) == str(root)
                        and not (m.isdev() or m.isfifo())]
                tf.extractall(dest, members=safe)
            _owner_writable(dest)
            if not quiet:
                ui.ok(f"applied {part.name}")
        else:
            gone = 0
            for line in part.read_text(encoding="utf-8").splitlines():
                f = dest / line
                if f.is_file() or f.is_symlink():
                    f.unlink(); gone += 1
            if not quiet:
                ui.ok(f"applied {part.name} ({gone} deletions)")
    if not quiet:
        ui.say(f"latest {name} is in {dest}")



# ---------------------------------------------------------------------- restore wizard
class Restore:
    """Guided restore onto a freshly flashed phone. Every step re-checks the phone, so re-running is safe."""

    def __init__(self, dev, bdir, args):
        self.dev, self.b, self.args, self.dry = dev, bdir, args, args.dry_run
        self.arch = bdir / "archives"
        self.st_path = bdir / "restore_state.json"
        self.st = json.loads(self.st_path.read_text()) if self.st_path.exists() else {"steps": {}, "apps": {}}
        self.prof = json.loads((bdir / "profile.json").read_text()) if (bdir / "profile.json").exists() else {}
        self.old = json.loads((bdir / "device.json").read_text()) if (bdir / "device.json").exists() else {}
        self.tmp = bdir / ".restore-tmp"
        self.need_reboot = False

    def save(self):
        if not self.dry:
            self.st_path.write_text(json.dumps(self.st, indent=1))

    # ---- primitives (all writes go through these, so --dry-run is honest)
    def w(self, cmd):
        if self.dry:
            print(f"     [dry-run] {cmd.strip()[:220]}"); return ""
        return self.dev.sh(cmd)

    def push(self, local, remote, sync=False):
        if self.dry:
            print(f"     [dry-run] adb push {'--sync ' if sync else ''}{local} {remote}"); return True
        r = subprocess.run([self.dev.adb, "-s", self.dev.serial, "push"] + (["--sync"] if sync else []) + [str(local), remote],
                           capture_output=True, text=True)
        if r.returncode:
            ui.warn(f"push {Path(local).name} failed: {(r.stderr or r.stdout).strip()[-200:]}")
        return r.returncode == 0

    def latest(self, name):
        """Latest state of an archive, extracted once into .restore-tmp/<name>."""
        dest = self.tmp / name
        if not (dest / ".complete").exists():
            rmtree(dest)
            ui.say(f"unpacking {name} on this computer…")
            extract(self.b, name, dest, quiet=True)
            (dest / ".complete").touch()
        return dest

    def member(self, name, path):
        """Tar header (owner/mode) of the newest copy of path inside an archive chain."""
        m = None
        for part in archive_chain(self.arch, name):
            if part.suffix == ".tar":
                with tarfile.open(part) as tf:
                    for x in tf:
                        if x.name == path:
                            m = x
            elif path in part.read_text(encoding="utf-8").splitlines():
                m = None
        return m

    def put_root_file(self, name, path, dest=None):
        """Copy one file from an archive to the same place on the phone with its original owner + mode."""
        dest = dest or "/" + path
        m = self.member(name, path)
        local = self.latest(name) / path
        if m is None or not local.exists():
            return "missing"
        if self.dev.sh(f"sha256sum '{dest}' 2>/dev/null").split()[:1] == [sha256_file(local)]:
            return "same"
        if not self.push(local, f"{DTMP}/rf"):
            return "failed"
        self.w(f"mkdir -p \"$(dirname '{dest}')\"; cp {DTMP}/rf '{dest}'; chown {m.uid}:{m.gid} '{dest}'; "
               f"chmod {m.mode & 0o7777:o} '{dest}'; restorecon '{dest}' 2>/dev/null; rm -f {DTMP}/rf")
        return "copied"

    def has(self, name):
        return (self.arch / f"{name}.tar").exists()

    def listing(self, name):
        p = self.arch / f"{name}.list"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def prompt(self, keys, default="s"):
        """keys: text like 'Enter = check again · s = skip'. Returns the lowercase answer ('' for Enter)."""
        return ui.ask(f"{keys}:", default).lower()

    def reboot(self):
        if self.dry:
            print("     [dry-run] adb reboot, wait for boot"); return
        ui.say("rebooting the phone — this takes a minute…")
        subprocess.run([self.dev.adb, "-s", self.dev.serial, "reboot"])
        time.sleep(8)
        subprocess.run([self.dev.adb, "-s", self.dev.serial, "wait-for-device"])
        while self.dev.sh("getprop sys.boot_completed", root=False).strip() != "1":
            time.sleep(2)
        time.sleep(5)
        self.dev.detect()
        self.need_reboot = False
        ui.ok("phone is back")

    def target_user(self, old_user):
        """Backups made in user N (Island/work profile) restore into whatever secondary user exists now."""
        if old_user == 0:
            return 0
        others = [u for u in self.dev.users if u != 0]
        return others[0] if others else None

    # ---- steps: each returns DONE / OPEN / SKIPPED / N/A
    def s_phone(self):
        d, old = self.dev, self.old
        now_dev = d.sh("getprop ro.product.device", root=False).strip()
        now_build = d.sh("getprop ro.build.display.id", root=False).strip()
        ui.ok(f"backup: {old.get('display', '?')} (taken {old.get('started', '?')})")
        ui.ok(f"phone now: {now_dev}, build {now_build}")
        if old.get("serial") and old["serial"] != d.serial:
            ui.warn("this is a DIFFERENT phone than the one backed up. Apps/media are fine; "
                    "the IMEI/calibration partition images must never be written to it.")
        if old.get("fingerprint", "").split("/")[2:3] and now_dev not in old.get("fingerprint", ""):
            ui.warn(f"the backup is from a different model ({old.get('fingerprint', '?').split('/')[2]})")
        if old.get("serial") == d.serial and old.get("display") == now_build and not self.dry:
            ui.warn("this phone has NOT been reflashed since the backup (same phone, same build).")
            print("     Restoring now would overwrite your CURRENT app data and settings with the older copies")
            print("     from the backup. Flash first, then run this. (Want a harmless preview? Use --dry-run.)")
            a = self.prompt(f"type {ui.B}overwrite{ui.N} to continue anyway · {ui.B}Enter{ui.N} = quit", "")
            if a != "overwrite":
                ui.say("nothing was changed."); sys.exit(0)
            return "DONE"
        a = self.prompt(f"Restore onto this phone? {ui.B}y{ui.N} = yes · {ui.B}q{ui.N} = quit", "y")
        if a not in ("y", "yes", ""):
            sys.exit(0)
        return "DONE"

    def s_root(self):
        while not self.dev.root:
            print("     Root isn't set up yet. Flash/patch your root solution (KernelSU / Magisk / APatch) for the NEW")
            print("     kernel, open its app, and allow root for 'Shell'. (Most later steps need root.)")
            a = self.prompt(f"{ui.B}Enter{ui.N} = I did it, check again · {ui.B}s{ui.N} = skip (only photos/files get restored)", "s")
            if a == "s":
                return "SKIPPED"
            self.dev.detect()
        ui.ok("root works")
        return "DONE"

    def s_modules(self):
        inv = self.b / "inventory" / "root_modules.txt"
        if not inv.exists():
            return "N/A"
        mods = {}
        for block in inv.read_text(encoding="utf-8").split("== ")[1:]:
            kv = dict(l.split("=", 1) for l in block.splitlines()[1:] if "=" in l)
            if "id" in kv:
                mods[kv["id"]] = kv
        while True:
            have = set(self.dev.sh("ls /data/adb/modules 2>/dev/null").split())
            missing = [m for i, m in mods.items() if i not in have]
            ui.ok(f"{len(mods) - len(missing)}/{len(mods)} root modules installed")
            if not missing:
                return "DONE"
            print("     Install these from fresh zips (root manager › Modules › Install from storage), then reboot:")
            for m in missing:
                print(f"       - {m.get('name', m['id'])}  {m.get('version', '')}"
                      + ("   (was DISABLED — install, then disable)" if m.get("STATE") == "DISABLED" else ""))
            a = self.prompt(f"{ui.B}Enter{ui.N} = I did it, check again · {ui.B}s{ui.N} = skip for now")
            if a == "s":
                return "OPEN"
            self.dev.detect()

    def s_rootconfig(self):
        if not self.has("root-adb"):
            return "N/A"
        idx = set(self.listing("root-adb"))
        want = self.prof.get("restore_root_files") or [
            "data/adb/tricky_store/keybox.xml", "data/adb/tricky_store/target.txt",
            "data/adb/tricky_store/security_patch.txt", "data/adb/pif.prop", "data/adb/pif.json"]
        want += [p for p in idx if re.search(r"data/adb/modules/playintegrityfix/custom\.pif\.(prop|json)$", p)]
        want = [p for p in dict.fromkeys(want) if p in idx]
        if not want:
            return "N/A"
        blocked = sorted({str(Path(p).parent) for p in want if not self.dev.exists("/" + str(Path(p).parent))})
        print("     Your root config: " + ", ".join(Path(p).name for p in want))
        if blocked:
            print("     Waiting for the module that owns: " + ", ".join(blocked) + " (install it in the modules step)")
            want = [p for p in want if str(Path(p).parent) not in blocked]
        a = self.prompt(f"Copy them back? {ui.B}Enter{ui.N} = yes · {ui.B}s{ui.N} = skip", "")
        if a == "s":
            return "OPEN"
        for p in want:
            r = self.put_root_file("root-adb", p)
            ui.ok(f"{Path(p).name}: " + {"same": "already identical", "copied": "restored"}.get(r, r))
            self.need_reboot |= r == "copied"
        return "OPEN" if blocked else "DONE"

    def s_lsposed(self):
        inv = self.b / "inventory"
        if not (inv / "lspd_modules_config.db").exists():
            return "N/A"
        if not self.dev.exists("/data/adb/lspd"):
            print("     The LSPosed (Zygisk) module isn't installed yet — finish the modules step, reboot, run again.")
            return "OPEN"
        mgr = self.prof.get("lsposed_manager")
        if mgr and not self.dev.sh(f"pm path {mgr}", root=False).strip():
            apks = sorted((self.b / "apks" / mgr).glob("*.apk"))
            print("     Install your LSPosed manager yourself" + (f":  adb install {apks[0]}" if apks else "."))
        try:
            con = sqlite3.connect(f"file:{inv / 'lspd_modules_config.db'}?mode=ro", uri=True)
            enabled = sorted({r[0] for r in con.execute("SELECT module_pkg_name FROM modules_state WHERE enabled=1")})
        except sqlite3.Error:
            enabled = []
        own = [p for p in enabled if not self.dev.sh(f"pm path {p}", root=False).strip()]
        if own:
            print(f"     Module apps not installed yet: {', '.join(own)}")
            if self.prompt(f"Install them from the backup? {ui.B}Enter{ui.N} = yes · {ui.B}s{ui.N} = skip", "") != "s":
                for p in own:
                    apks = sorted((self.b / "apks" / p).glob("*.apk"))
                    if not apks:
                        ui.warn(f"{p}: no APK in the backup"); continue
                    if self.dry:
                        print(f"     [dry-run] adb install-multiple {' '.join(a.name for a in apks)}"); continue
                    r = subprocess.run([self.dev.adb, "-s", self.dev.serial, "install-multiple"] + [str(a) for a in apks],
                                       capture_output=True, text=True)
                    (ui.ok if r.returncode == 0 else ui.warn)(f"{p}: " + ("installed" if r.returncode == 0 else r.stdout.strip()[-120:]))
        q = ("SELECT m.module_pkg_name, s.user_id, s.app_pkg_name FROM modules m JOIN scope s ON s.module_pkg_name=m.module_pkg_name "
             "JOIN modules_state st ON st.module_pkg_name=m.module_pkg_name AND st.user_id=s.user_id WHERE st.enabled=1")
        def scopes(db):
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                return {tuple(r) for r in con.execute(q).fetchall()}
            except sqlite3.Error:
                return set()
        want = scopes(inv / "lspd_modules_config.db")
        while True:
            cur_dir = self.tmp / "lspd-now"; cur_dir.mkdir(parents=True, exist_ok=True)
            for ext in ["", "-wal", "-shm"]:
                (cur_dir / f"m.db{ext}").write_bytes(self.dev.sh(f"cat /data/adb/lspd/config/modules_config.db{ext} 2>/dev/null", binary=True))
            missing = sorted(want - scopes(cur_dir / "m.db"))
            ui.ok(f"{len(want) - len(missing)}/{len(want)} LSPosed module scopes set")
            if not missing:
                return "DONE"
            print("     In the LSPosed manager: enable each module and tick these apps (then reboot):")
            for mod in sorted({m for m, _u, _a in missing}):
                apps = [f"{a}{'' if u == 0 else f' (user {u})'}" for m, u, a in missing if m == mod]
                print(f"       - {mod}: {', '.join(apps)}")
            a = self.prompt(f"{ui.B}Enter{ui.N} = I did it, check again · {ui.B}s{ui.N} = skip for now")
            if a == "s":
                return "OPEN"

    def s_network(self):
        if not self.has("system-state"):
            return "N/A"
        idx = set(self.listing("system-state"))
        files = [p for p in ["data/misc/apexdata/com.android.wifi/WifiConfigStore.xml",
                             "data/misc/apexdata/com.android.wifi/WifiConfigStoreSoftAp.xml",
                             "data/misc/wifi/WifiConfigStore.xml",
                             "data/misc/bluedroid/bt_config.conf", "data/misc/adb/adb_keys"] if p in idx]
        if not files:
            return "N/A"
        nets = (self.b / "exports" / "wifi_networks.txt")
        n = len(nets.read_text(encoding="utf-8").splitlines()) if nets.exists() else "?"
        print(f"     Saved WiFi networks ({n}), hotspot settings, Bluetooth pairings and this computer's USB-debugging key.")
        print("     WiFi and Bluetooth are switched off for a moment, then the phone reboots to apply them.")
        if self.prompt(f"{ui.B}Enter{ui.N} = do it · {ui.B}s{ui.N} = skip", "") == "s":
            return "OPEN"
        self.w("svc wifi disable; svc bluetooth disable 2>/dev/null || cmd bluetooth_manager disable")
        for p in files:
            r = self.put_root_file("system-state", p)
            ui.ok(f"{Path(p).name}: " + {"same": "already identical", "copied": "restored"}.get(r, r))
            self.need_reboot |= r == "copied"
        if self.need_reboot:
            self.reboot()
        self.w("svc wifi enable; svc bluetooth enable 2>/dev/null || cmd bluetooth_manager enable")
        print(f"     If WiFi didn't come back: names + passwords are in {nets}")
        return "DONE"

    def s_media(self):
        names = [n for n in ["sd-camera", "sd-download", "sd-rest", "sd-android-data"] if self.has(n)]
        if not names:
            return "N/A"
        done = self.st.setdefault("media_done", [])
        todo = [n for n in names if n not in done]
        if not todo:
            return "DONE"
        sizes = {n: (self.arch / f"{n}.tar").stat().st_size for n in todo}
        print("     Photos, videos, screenshots, downloads, documents, call recordings, everything else on storage:")
        for n in todo:
            print(f"       - {n}  ({human(sizes[n])})")
        if self.prompt(f"{ui.B}Enter{ui.N} = copy to the phone · {ui.B}s{ui.N} = skip", "") == "s":
            return "OPEN"
        installed = set(self.dev.packages(0, third_party=False)) | set(self.dev.packages(0))
        for n in todo:
            if self.dry:
                print(f"     [dry-run] unpack {n} here, then adb push --sync each top-level folder into /sdcard/"); continue
            root = self.latest(n) / "data" / "media" / "0"
            if not root.exists():
                done.append(n); continue
            if n == "sd-android-data":   # only folders of apps that exist now (others would be orphans)
                tops = [p for p in (root / "Android" / "data").iterdir() if p.name in installed]
                dest = "/sdcard/Android/data/"
            else:
                tops, dest = list(root.iterdir()), "/sdcard/"
            ok_ = all(self.push(t, dest, sync=True) for t in tops)
            if ok_:
                done.append(n); self.save()
                rmtree(self.tmp / n)
                ui.ok(f"{n}: copied ({len(tops)} item(s))")
        self.w("content call --uri content://media --method scan_volume --arg external_primary >/dev/null 2>&1")
        return "DONE" if set(names) <= set(done) else "OPEN"

    def s_whatsapp(self):
        jobs = []   # (_, old_user, archive, package)
        for u in self.old.get("users", [0]):
            name = "sd-messengers" if u == 0 else f"user{u}-media"
            found = {m.group(1) for m in (re.match(rf"data/media/{u}/Android/media/(com\.whatsapp(?:\.w4b)?)/", p)
                                          for p in self.listing(name)) if m}
            jobs += [("WA", u, name, pkg) for pkg in sorted(found)]
        if not jobs:
            return "N/A"
        status = "DONE"
        for _wa, ou, name, pkg in jobs:
            nm = "WhatsApp Business" if pkg.endswith("w4b") else "WhatsApp"
            where = "" if ou == 0 else " inside your work profile (Island)"
            print(f"\n     {ui.B}{nm}{where}{ui.N}")
            while True:
                tu = self.target_user(ou)
                if tu is None:
                    print("     Set up your work profile first: install Island (or Shelter) from the Play Store and create the profile.")
                elif not self.dev.sh(f"pm path --user {tu} {pkg}", root=False).strip():
                    print(f"     Install {nm}{where} from the Play Store — but DON'T open it yet.")
                else:
                    ce = f"/data/data/{pkg}" if tu == 0 else f"/data/user/{tu}/{pkg}"
                    if self.dev.exists(f"{ce}/databases/msgstore.db"):
                        ui.ok(f"{nm}{where}: chats are restored"); break
                    if not self.st.get(f"wa_pushed_{tu}_{pkg}"):
                        self._push_wa(name, ou, tu, pkg)
                        self.st[f"wa_pushed_{tu}_{pkg}"] = True; self.save()
                    print(f"     Now open {nm}{where}, verify your number, and tap RESTORE when it offers your backup.")
                    print("     (If it doesn't find it: turn mobile data + WiFi off for a moment and retry. Google Drive works too.)")
                a = self.prompt(f"{ui.B}Enter{ui.N} = done, check again · {ui.B}s{ui.N} = skip for now")
                if a == "s":
                    status = "OPEN"; break
                self.dev.detect()
                if self.dry:
                    break
        return status

    def _push_wa(self, name, ou, tu, pkg):
        if self.dry:
            print(f"     [dry-run] unpack {name}, copy Android/media/{pkg} into user {tu}'s storage"); return
        src = self.latest(name) / "data" / "media" / str(ou) / "Android" / "media" / pkg
        if tu == 0:
            self.push(src, "/sdcard/Android/media/", sync=True)
        else:   # another user's storage: stage it, then copy as root and match the folder's owner
            stage = f"{DTMP}/wa"
            self.w(f"rm -rf {stage}; mkdir -p {stage}")
            self.push(src, stage + "/")
            base = f"/data/media/{tu}/Android/media"
            self.w(f"mkdir -p {base}; cp -a {stage}/{pkg} {base}/; "
                   f"chown -R $(stat -c %u:%g {base}) {base}/{pkg}; restorecon -RF {base}/{pkg}; rm -rf {stage}")
        ui.ok(f"{pkg}: chat backup + media copied to the phone")

    def s_sms(self):
        ex = self.b / "exports"
        if not (ex / "sms.txt").exists():
            return "N/A"
        want_sms = (ex / "sms.txt").read_text(encoding="utf-8").count("Row:")
        want_calls = (ex / "call_log.txt").read_text(encoding="utf-8").count("Row:") if (ex / "call_log.txt").exists() else 0
        while True:
            have_sms = self.dev.sh("content query --uri content://sms --projection _id").count("Row:")
            have_calls = self.dev.sh("content query --uri content://call_log/calls --projection _id").count("Row:")
            ui.ok(f"messages on phone: {have_sms}/{want_sms} · call log: {have_calls}/{want_calls}")
            if have_sms >= want_sms * 0.95 and have_calls >= want_calls * 0.95:
                return "DONE"
            print("     Install 'SMS Backup & Restore', open it › Restore › pick your XML export (messages + calls).")
            print(f"     No XML export? Your messages are readable in {ex / 'sms.txt'}.")
            a = self.prompt(f"{ui.B}Enter{ui.N} = done, check again · {ui.B}s{ui.N} = skip")
            if a == "s":
                return "OPEN"

    def s_apps(self):
        ref = set(KEYSTORE_BOUND) | set(self.prof.get("reference_apps", []))
        pkgs = sorted(t.name[4:-4] for t in self.arch.glob("app-*.tar") if t.name[4:-4] not in ref)
        if not pkgs:
            return "N/A"
        apps = self.st.setdefault("apps", {})
        todo = [p for p in pkgs if apps.get(p) != "DONE"]
        if not todo:
            ui.ok(f"all {len(pkgs)} apps restored"); return "DONE"
        print(f"     {len(todo)} app(s) whose settings/data come back from the backup:")
        print("     " + ", ".join(todo))
        print("     Missing ones are installed from the backup's APKs where possible (otherwise: Play Store first).")
        if self.prompt(f"{ui.B}Enter{ui.N} = restore them · {ui.B}s{ui.N} = skip", "") == "s":
            return "OPEN"
        for p in todo:
            ok_, msg = restore_app_data(self.dev, self.b, p, 0, dry=self.dry)
            (ui.ok if ok_ else ui.warn)(msg)
            if ok_ and not self.dry:
                apps[p] = "DONE"; self.save()
        left = [p for p in pkgs if apps.get(p) != "DONE"]
        return "DONE" if not left else "OPEN"

    def s_logins(self):
        items = []
        allpk = set()
        for f in (self.b / "inventory").glob("packages_user*.txt"):
            allpk |= set(re.findall(r"package:(\S+)", f.read_text(encoding="utf-8")))
        auth = [a for a in AUTHENTICATORS if a in allpk]
        if auth:
            items.append(("Authenticators", f"{', '.join(auth)}: import your export / restore from the app's cloud backup."))
        items.append(("Google accounts", "Add your other Google accounts (Settings › Passwords & accounts)."))
        banks = sorted(p for p in allpk if BANK_HINT.search(p)) + self.prof.get("banking_apps", [])
        if banks:
            items.append(("Banking / UPI", "Log in and re-register the device (SIM verification): "
                          + ", ".join(sorted(set(banks))[:8]) + ("…" if len(set(banks)) > 8 else "")))
        nbu = self.b / "NOT_BACKED_UP.txt"
        if nbu.exists():
            items.append(("Other apps", f"Install + log in to the rest as you need them (list: {nbu.name})."))
        st = self.st.setdefault("logins", {})
        todo = [(t, l) for t, l in items if st.get(t) != "DONE"]
        for t, l in todo:
            print(f"     {ui.B}{t}{ui.N}: {l}")
            if self.prompt(f"done? {ui.B}y{ui.N} / {ui.B}Enter{ui.N} = later", "") in ("y", "yes"):
                st[t] = "DONE"
        self.save()
        return "DONE" if all(st.get(t) == "DONE" for t, _ in items) else "OPEN"

    STEPS = [("phone", "Check the phone", False), ("root", "Root access", False),
             ("modules", "Root modules", True), ("rootconfig", "Root config (keybox, TrickyStore, PIF)", True),
             ("lsposed", "LSPosed modules + scopes", True), ("network", "WiFi, Bluetooth, USB-debugging key", True),
             ("media", "Photos, videos, downloads, files", False), ("whatsapp", "WhatsApp chats", True),
             ("sms", "Messages + call log", False), ("apps", "App data", True), ("logins", "Accounts + logins", False)]

    def run(self):
        steps = self.st["steps"]
        n = len(self.STEPS)
        for i, (key, title, needs_root) in enumerate(self.STEPS, 1):
            if steps.get(key) in ("DONE", "N/A") and key not in ("phone", "root") and not self.args.redo:
                ui.ok(f"[{i}/{n}] {title}: done"); continue
            ui.hdr(f"[{i}/{n}] {title}")
            if needs_root and not self.dev.root:
                ui.warn("needs root — skipped (finish the root step, then run again)")
                steps[key] = "OPEN"; continue
            steps[key] = getattr(self, f"s_{key}")()
            self.save()
        if self.need_reboot:
            self.reboot()
        self.w(f"rm -rf {DTMP}")
        rmtree(self.tmp)
        left = [t for k, t, _r in self.STEPS if steps.get(k) not in ("DONE", "N/A")]
        print()
        if self.dry:
            ui.say("dry run finished — nothing was written to the phone")
        elif not left:
            ui.say(f"{ui.G}{ui.B}✅ RESTORE COMPLETE.{ui.N} Reboot once more and enjoy the new ROM.")
            print("     Last check: make a call / use mobile data. No signal or IMEI after the flash? Don't panic —")
            print(f"     the IMEI/calibration images are in {self.b / 'partitions'} (see README.txt there; same phone only).")
        else:
            ui.say(f"{ui.Y}{ui.B}Almost there.{ui.N} Still open: " + ", ".join(left))
            print(f"     Run the {ui.B}same command again{ui.N} whenever you're ready — finished steps are skipped.")


def find_backup(model):
    found = sorted((d for d in (Path.home() / "android-backups").glob(f"{model}-*") if (d / "state.json").exists()),
                   key=lambda d: d.stat().st_mtime)
    return found[-1] if found else None


# ----------------------------------------------------------------------------- main
def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)  # live output even when redirected to a file
    except AttributeError:
        pass
    if len(sys.argv) > 1 and sys.argv[1] == "restore-app":
        ap = argparse.ArgumentParser(prog="droidvault restore-app")
        ap.add_argument("_cmd"); ap.add_argument("backup_dir", type=Path); ap.add_argument("pkg")
        ap.add_argument("--user", type=int, default=0); ap.add_argument("-s", "--serial"); ap.add_argument("--adb", default="adb")
        a = ap.parse_args()
        restore_app(Device(shutil.which(a.adb) or a.adb, a.serial), a.backup_dir, a.pkg, a.user); return

    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        ap = argparse.ArgumentParser(prog="droidvault restore",
                                     description="Guided restore onto a freshly flashed phone. Re-run any time; done steps are skipped.")
        ap.add_argument("_cmd"); ap.add_argument("backup_dir", type=Path, nargs="?", help="default: latest backup of this phone")
        ap.add_argument("-s", "--serial"); ap.add_argument("--adb", default="adb")
        ap.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing to the phone")
        ap.add_argument("--redo", action="store_true", help="go through steps already marked done")
        a = ap.parse_args()
        adb = shutil.which(a.adb) or a.adb
        if not shutil.which(adb):
            ui.die("adb isn't installed (macOS: brew install android-platform-tools · Windows: winget install Google.PlatformTools)")
        print(f"\n{ui.B}droidvault restore{ui.N} — puts your backup back. Safe to stop (Ctrl-C) and re-run any time."
              + ("  [DRY RUN: nothing is written]" if a.dry_run else "") + "\n")
        dev = Device(adb, a.serial); dev.detect()
        model = dev.sh("getprop ro.product.device", root=False).strip()
        bdir = a.backup_dir or find_backup(model)
        if not bdir or not (bdir / "state.json").exists():
            ui.die(f"no backup found for {model} under ~/android-backups (pass the folder: droidvault.py restore <dir>)")
        ui.log_path = bdir / "restore.log"
        ui.say(f"using backup {bdir}")
        Restore(dev, bdir, a).run(); return

    if len(sys.argv) > 1 and sys.argv[1] == "extract":
        ap = argparse.ArgumentParser(prog="droidvault extract")
        ap.add_argument("_cmd"); ap.add_argument("backup_dir", type=Path); ap.add_argument("archive")
        ap.add_argument("dest", type=Path)
        a = ap.parse_args()
        extract(a.backup_dir, a.archive, a.dest); return
    update = len(sys.argv) > 1 and sys.argv[1] == "update"
    if update:
        sys.argv.pop(1)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--serial"); ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--adb", default="adb", help="path to adb (default: from PATH)")
    ap.add_argument("--profile", type=Path, help="JSON profile with device/user-specific lists")
    ap.add_argument("--only", help="comma-separated phases to run")
    ap.add_argument("--phases", action="store_true", help="list phases and exit")
    ap.add_argument("--yes", action="store_true", help="no prompts (checklist items recorded UNCONFIRMED)")
    ap.add_argument("--redo", action="store_true", help="refresh archives already present")
    ap.add_argument("--all-apps", action="store_true", help="data for every user app, not just the selection")
    ap.add_argument("--all-apks", action="store_true", help="APKs for every user app, not just sideloaded ones")
    ap.add_argument("--super", action="store_true", help="also image 'super' (system; large, rollback only)")
    ap.add_argument("--deep", action="store_true", help="update: re-hash every archive, not just this run's changes")
    ap.add_argument("--new", action="store_true", help="start a new backup folder instead of continuing the latest one")
    ap.add_argument("--pick-apps", action="store_true", help="show the app picker again to add apps")
    args = ap.parse_args()
    args.update = update
    if args.phases:
        print(" ".join(PHASES)); return
    adb = shutil.which(args.adb) or args.adb
    if not shutil.which(adb):
        ui.die("adb (Android platform-tools) isn't installed. Install it, then run this again:\n"
               "   macOS:   brew install android-platform-tools\n"
               "   Windows: winget install Google.PlatformTools\n"
               "   Linux:   sudo apt install adb   (or your distro's package)")
    print(f"\n{ui.B}droidvault{ui.N} — backup before a wipe. Safe to stop (Ctrl-C) and re-run any time.\n")
    dev = Device(adb, args.serial)
    dev.detect()
    while not dev.root and not ui.yes:
        print(f"  {ui.Y}No root access yet.{ui.N} In KernelSU / Magisk / APatch › Superuser, allow root for 'Shell'"
              " (if a popup appeared on the phone, tap Allow/Grant).")
        a = ui.ask(f"{ui.B}Enter{ui.N} = I did it, check again  ·  {ui.B}n{ui.N} = continue without root (much less gets saved):", "").lower()
        if a == "n":
            break
        dev.detect()
    model = dev.sh("getprop ro.product.device", root=False).strip() or "android"
    # profile: explicit path, a bare name, or auto-detected <codename>.json. Personal profiles belong in
    # ~/.droidvault/profiles/ (outside any git checkout); profiles/ next to the script is checked second.
    dirs = [Path.home() / ".droidvault" / "profiles", Path(__file__).resolve().parent / "profiles"]
    if args.profile and not args.profile.exists():
        args.profile = next((d / args.profile.name for d in dirs if (d / args.profile.name).exists()), args.profile)
    if not args.profile:
        args.profile = next((d / f"{model}.json" for d in dirs if (d / f"{model}.json").exists()), None)
    if args.profile and not args.profile.exists():
        ui.die(f"profile {args.profile} not found")
    profile = json.loads(args.profile.read_text()) if args.profile else {}
    out = args.out
    if not out and not args.new:
        found = sorted((d for d in (Path.home() / "android-backups").glob(f"{model}-*") if (d / "state.json").exists()),
                       key=lambda d: d.stat().st_mtime)
        out = found[-1] if found else None
    out = out or Path.home() / "android-backups" / f"{model}-{dt.date.today():%F}"
    args.update = args.update or ((out / "state.json").exists() and not args.redo)
    out.mkdir(parents=True, exist_ok=True)
    ui.log_path, ui.warn_path, ui.yes = out / "backup.log", out / "WARNINGS.txt", args.yes
    if ui.warn_path.exists():
        ui.warn_path.unlink()
    ui.say(f"phone: {model}  ·  profile: {args.profile.name if args.profile else 'none (auto-detect only)'}")
    if args.update:
        st0 = json.loads((out / "state.json").read_text())
        last = max([m.get("updated", m.get("time", "")) for m in st0.get("manifest", {}).values()] or ["?"])
        ui.say(f"continuing your backup in {out} (last change {last}) — only what changed gets copied")
    else:
        ui.say(f"new backup in {out} — the first run copies everything (roughly 15–30 min for a full phone)")
    b = Backup(dev, out, args, profile)
    phases = [p.strip() for p in args.only.split(",")] if args.only else list(PHASES)
    for p in phases:
        if p not in PHASES:
            ui.die(f"unknown phase {p}; one of: {' '.join(PHASES)}")
    if "preflight" not in phases:
        phases.insert(0, "preflight")
    t0 = time.time()
    for p in phases:
        getattr(b, f"p_{p}")()
    if args.update:
        ui.say(f"done in {int(time.time() - t0)} s — "
               + ("new changes were added to the backup" if getattr(b, "changed", False) else "nothing had changed"))
    if "report" not in phases:
        dev.sh("settings put global stay_on_while_plugged_in 0", root=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.clear(); print("\ninterrupted — re-run the same command to resume")
        sys.exit(130)
