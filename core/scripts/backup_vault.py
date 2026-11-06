# -*- coding: utf-8 -*-
"""Backup: a dated zip of the vault (plus the scripts folder and the boot file), written OUTSIDE the vault, with the newest N kept.

Why: a snapshot history inside the vault (vault_snapshot.py) does not survive losing the vault folder or the disk it sits on. This writes a separate archive
somewhere else, so there is always a copy to restore from. Restore by opening the .zip with any unzip tool (an encrypted one: decrypt it first, see below).

    python backup_vault.py                          # one backup into the backup folder
    python backup_vault.py --keep 10                # ...and afterwards delete all but the newest 10 backups made by this tool
    python backup_vault.py --dry-run                # print what would be archived and pruned, write nothing
    python backup_vault.py --encrypt                # encrypt the archive; the passphrase comes from the environment variable HARNESS_BACKUP_PASSPHRASE
    python backup_vault.py --encrypt --prompt-passphrase   # ...or is typed at a prompt (only works in a terminal, never in a scheduled job)
    python backup_vault.py --decrypt X.zip.enc --out X.zip  # turn an encrypted backup back into a plain zip (same passphrase)
    python backup_vault.py --job                    # scheduled run: problems become alerts, the exit code is always 0

What goes in: the vault folder (HARNESS_VAULT, default ROOT/vault), ROOT/scripts, ROOT/CLAUDE.md when they exist, and anything named with --include PATH
(repeatable). Left out: __pycache__, node_modules, .DS_Store, Thumbs.db, *.pyc and anything named with --exclude PATTERN. Symbolic links are never followed or stored.
Inside the zip the files sit under vault/, scripts/, CLAUDE.md, extra-<name>/, plus BACKUP-MANIFEST.json (every file's size and SHA-256, used by verify_backup.py).

Where it goes: --dest DIR, else the environment variable HARNESS_BACKUP_DIR, else a folder called assistant-backups in your home folder. It refuses a folder inside the vault
(a backup inside what it protects protects nothing). Files are named assistant-backup-YYYY-MM-DD_HHMMSS.zip (.enc added when encrypted) and are written under a .partial
name and renamed when complete, so a crash never leaves a half archive that looks real.

Keeping N: nothing is ever deleted unless you pass --keep N (N >= 1). Then only files whose names match the pattern above, in the backup folder, beyond the newest N, go.
If the vault folder is missing (a drive not mounted, a typo) the run is skipped, so a missing vault can never rotate your good backups out.

Encryption (optional, off unless --encrypt): the passphrase is never taken from the command line. It is read from the environment variable named by --passphrase-env
(default HARNESS_BACKUP_PASSPHRASE) or typed at a prompt with --prompt-passphrase; at least 8 characters. If --encrypt is given and no passphrase can be found the run fails
and writes nothing (it never quietly falls back to a plain archive). The format is built only from Python's standard library: scrypt key derivation, a SHAKE-256 keystream
and an HMAC-SHA256 tag (encrypt-then-MAC, header authenticated, random salt and nonce per file). It is NOT AES and has not been audited by cryptographers: it keeps a lost
or stolen drive unreadable to casual and automated access, but for high-value data also put the backup folder on an encrypted disk or use a vetted tool. Keep the passphrase
somewhere safe: without it the backup cannot be opened and there is no recovery.

Kill switch: the file logs/backup_vault_off.txt. State: logs/backup_vault_state.json. Failures raise an alert through notify.py.
Environment: HARNESS_ROOT, HARNESS_LOGS, HARNESS_VAULT, HARNESS_BACKUP_DIR, HARNESS_BACKUP_PASSPHRASE.
Pure core (unit tested): build_sources, iter_entries, write_zip, EncryptWriter, decrypt_file, list_backups, prune, make_backup."""
import argparse
import fnmatch
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()
SCRIPTS = Path(__file__).parent.resolve()

PREFIX = "assistant-backup-"
NAME_RE = re.compile(r"^assistant-backup-(\d{4}-\d{2}-\d{2}_\d{6})(?:-(\d+))?\.zip(\.enc)?$")
MANIFEST = "BACKUP-MANIFEST.json"
DEFAULT_EXCLUDES = ("__pycache__", "node_modules", ".DS_Store", "Thumbs.db", "*.pyc")
PASS_ENV = "HARNESS_BACKUP_PASSPHRASE"
MIN_PASSPHRASE = 8
STATE_NAME = "backup_vault_state.json"
OFF_NAME = "backup_vault_off.txt"
ZIP64_HINT = 1 << 30
NO_DATE_BEFORE = 315619200      # 1980-01-02, the earliest date a zip entry can carry
NO_DATE_AFTER = 4102444800      # year 2100

# encryption format: MAGIC + log2(n) + r + p + salt(16) + nonce(16) + ciphertext + HMAC tag(32)
MAGIC = b"ASBK\x01"
HEADER_LEN = len(MAGIC) + 3 + 16 + 16
TAG_LEN = 32
CHUNK = 1 << 20
SCRYPT_LOG2N, SCRYPT_R, SCRYPT_P = 15, 8, 1
MAX_LOG2N, MAX_R, MAX_P = 18, 16, 4


class BackupError(Exception):
    pass


def logs_dir():
    return Path(os.environ.get("HARNESS_LOGS") or Path(os.environ.get("HARNESS_ROOT") or ROOT) / "logs")


def root_dir():
    return Path(os.environ.get("HARNESS_ROOT") or ROOT)


def vault_dir():
    return Path(os.environ.get("HARNESS_VAULT") or root_dir() / "vault")


def backup_dir(cli_value=None):
    """--dest, else HARNESS_BACKUP_DIR, else ~/assistant-backups."""
    chosen = cli_value or os.environ.get("HARNESS_BACKUP_DIR")
    return Path(chosen).expanduser() if chosen else Path.home() / "assistant-backups"


# ---------------------------------------------------------------- small helpers

def is_within(path, parent):
    """True when path is parent or lies below it (both resolved, case-folded where the file system is)."""
    try:
        a = os.path.normcase(os.path.realpath(str(path)))
        b = os.path.normcase(os.path.realpath(str(parent)))
        return a == b or a.startswith(b.rstrip("\\/") + os.sep)
    except (OSError, ValueError):
        return False


def save_json(path, data):
    """Atomic write: temp file in the same folder, then rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_json(path, cap=1 << 20):
    try:
        p = Path(path)
        if p.stat().st_size > cap:
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def alert(text, dedupe=None, kind=None, tier=1, value=4):
    """One way to tell the owner: notify.alert. Never raises."""
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source="backup_vault", intent_kind=kind, tier=tier, value=value, dedupe=dedupe, logs=logs_dir())
    except Exception:
        pass


# ---------------------------------------------------------------- passphrase and encryption

def get_passphrase(env_name=PASS_ENV, prompt=False, confirm=False):
    """The passphrase from the environment variable, else (prompt=True and a terminal) typed in. -> str or None. Never from argv."""
    name = env_name or PASS_ENV
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise BackupError("--passphrase-env must be the NAME of an environment variable, not the passphrase itself")
    value = os.environ.get(name, "")
    if value:
        return value
    if prompt and sys.stdin is not None and sys.stdin.isatty():
        first = getpass.getpass("Backup passphrase: ")
        if confirm and getpass.getpass("Again: ") != first:
            raise BackupError("the two passphrases differ")
        return first or None
    return None


def check_passphrase(passphrase):
    if not passphrase or len(passphrase) < MIN_PASSPHRASE:
        raise BackupError("the passphrase must be at least %d characters" % MIN_PASSPHRASE)


def derive_keys(passphrase, salt, log2n, r, p):
    """-> (encryption key, mac key), 32 bytes each, from scrypt."""
    if not hasattr(hashlib, "scrypt"):
        raise BackupError("this Python was built without scrypt, so encrypted backups are not available")
    if not (1 <= log2n <= MAX_LOG2N and 1 <= r <= MAX_R and 1 <= p <= MAX_P):
        raise BackupError("the file asks for key-derivation settings outside the safe range")
    n = 1 << log2n
    dk = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=256 * n * r * p + (16 << 20), dklen=64)
    return dk[:32], dk[32:]


def _keystream(enc_key, nonce, counter, length):
    return hashlib.shake_256(enc_key + nonce + counter.to_bytes(8, "big")).digest(length)


def _xor(data, stream):
    return (int.from_bytes(data, "little") ^ int.from_bytes(stream, "little")).to_bytes(len(data), "little")


class EncryptWriter:
    """A write-only file object that encrypts what is written to it into `fileobj` (zipfile can write straight into it, so no plain copy ever touches the disk)."""

    def __init__(self, fileobj, passphrase):
        check_passphrase(passphrase)
        salt, self._nonce = secrets.token_bytes(16), secrets.token_bytes(16)
        header = MAGIC + bytes([SCRYPT_LOG2N, SCRYPT_R, SCRYPT_P]) + salt + self._nonce
        self._enc, mac_key = derive_keys(passphrase, salt, SCRYPT_LOG2N, SCRYPT_R, SCRYPT_P)
        self._mac = hmac.new(mac_key, header, hashlib.sha256)
        self._f = fileobj
        self._f.write(header)
        self._buf = bytearray()
        self._counter = 0
        self._closed = False

    def write(self, data):
        if self._closed:
            raise ValueError("write to a closed EncryptWriter")
        self._buf += data
        while len(self._buf) >= CHUNK:
            self._emit(bytes(self._buf[:CHUNK]))
            del self._buf[:CHUNK]
        return len(data)

    def _emit(self, plain):
        cipher = _xor(plain, _keystream(self._enc, self._nonce, self._counter, len(plain)))
        self._counter += 1
        self._mac.update(cipher)
        self._f.write(cipher)

    def flush(self):
        self._f.flush()

    def close(self):
        """Write the last block and the tag. The underlying file stays open (the caller closes it)."""
        if self._closed:
            return
        if self._buf:
            self._emit(bytes(self._buf))
            self._buf.clear()
        self._f.write(self._mac.digest())
        self._f.flush()
        self._closed = True


def is_encrypted_file(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def decrypt_file(src, dst, passphrase):
    """Check the tag over the whole file first, then write the plain zip to dst (atomically). A wrong passphrase and a damaged file look the same: nothing is written."""
    src, dst = Path(src), Path(dst)
    size = src.stat().st_size
    with open(src, "rb") as f:
        header = f.read(HEADER_LEN)
        if len(header) < HEADER_LEN or header[:len(MAGIC)] != MAGIC:
            raise BackupError("not an encrypted backup made by this tool")
        if size < HEADER_LEN + TAG_LEN:
            raise BackupError("the encrypted file is truncated")
        o = len(MAGIC)
        log2n, r, p = header[o], header[o + 1], header[o + 2]
        salt, nonce = header[o + 3:o + 19], header[o + 19:o + 35]
        enc_key, mac_key = derive_keys(passphrase, salt, log2n, r, p)
        cipher_len = size - HEADER_LEN - TAG_LEN
        mac = hmac.new(mac_key, header, hashlib.sha256)
        left = cipher_len
        while left:
            block = f.read(min(CHUNK, left))
            if not block:
                raise BackupError("the encrypted file is truncated")
            mac.update(block)
            left -= len(block)
        if not hmac.compare_digest(f.read(TAG_LEN), mac.digest()):
            raise BackupError("wrong passphrase, or the file is damaged or was changed")
        f.seek(HEADER_LEN)
        tmp = dst.with_name(dst.name + ".partial.%d" % os.getpid())
        try:
            with open(tmp, "wb") as out:
                counter, left = 0, cipher_len
                while left:
                    block = f.read(min(CHUNK, left))
                    out.write(_xor(block, _keystream(enc_key, nonce, counter, len(block))))
                    counter += 1
                    left -= len(block)
            os.replace(tmp, dst)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- what goes into the archive

def safe_label(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("._") or "item"


def build_sources(vault, root, include=(), with_scripts=True, with_boot=True):
    """-> ([(label, Path)], [warnings]). The vault, root/scripts, root/CLAUDE.md and the --include paths that exist."""
    out, notes = [], []
    if Path(vault).is_dir():
        out.append(("vault", Path(vault)))
    if with_scripts and (Path(root) / "scripts").is_dir():
        out.append(("scripts", Path(root) / "scripts"))
    boot = Path(root) / "CLAUDE.md"
    if with_boot and boot.is_file():
        out.append(("CLAUDE.md", boot))
    used = {label for label, _ in out}
    for extra in include:
        p = Path(extra).expanduser()
        if not p.exists():
            notes.append("--include %s does not exist: left out" % p)
            continue
        label, k = "extra-" + safe_label(p.name), 2
        base = label
        while label in used:
            label, k = "%s-%d" % (base, k), k + 1
        used.add(label)
        out.append((label, p))
    return out, notes


def _excluded(name, patterns):
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def iter_entries(sources, excludes=DEFAULT_EXCLUDES, skip_paths=()):
    """Yield (archive name, Path) for every regular file. Symbolic links, excluded names and the folders in skip_paths are not entered."""
    skip = [os.path.normcase(os.path.realpath(str(s))) for s in skip_paths]
    for label, src in sources:
        src = Path(src)
        if src.is_file() and not src.is_symlink():
            if not _excluded(src.name, excludes):
                yield label, src
            continue
        for dirpath, dirnames, filenames in os.walk(src, topdown=True, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if not _excluded(d, excludes)
                                 and not os.path.islink(os.path.join(dirpath, d))
                                 and os.path.normcase(os.path.realpath(os.path.join(dirpath, d))) not in skip)
            rel = Path(dirpath).relative_to(src).as_posix()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                if _excluded(fn, excludes) or os.path.islink(full):
                    continue
                yield "/".join(x for x in (label, "" if rel == "." else rel, fn) if x), Path(full)


def _zip_time(mtime):
    t = time.localtime(min(max(mtime, NO_DATE_BEFORE), NO_DATE_AFTER))
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def _add_file(zf, arc, path):
    """Store one file, hashing it on the way. -> manifest row. An unreadable file raises before anything is written."""
    with open(path, "rb") as src:
        st = os.fstat(src.fileno())
        info = zipfile.ZipInfo(arc, date_time=_zip_time(st.st_mtime))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (st.st_mode & 0xFFFF) << 16
        info.file_size = st.st_size
        digest, total, partial = hashlib.sha256(), 0, False
        with zf.open(info, "w", force_zip64=st.st_size >= ZIP64_HINT) as dst:
            try:
                while True:
                    block = src.read(CHUNK)
                    if not block:
                        break
                    digest.update(block)
                    dst.write(block)
                    total += len(block)
            except OSError:
                partial = True
    row = {"p": arc, "s": total, "h": digest.hexdigest()}
    if partial:
        row["partial"] = True
    return row


def write_zip(fileobj, entries, now=None):
    """Write the archive to fileobj. -> {"files": n, "bytes": n, "skipped": [..], "manifest": {...}}. A file that cannot be read is skipped and listed, not fatal."""
    rows, skipped = [], []
    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for arc, path in entries:
            try:
                row = _add_file(zf, arc, path)
            except OSError as e:
                skipped.append("%s (%s)" % (arc, type(e).__name__))
                continue
            rows.append(row)
            if row.get("partial"):
                skipped.append("%s (read error part way)" % arc)
        manifest = {"format": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now if now is not None else time.time())), "files": rows}
        zf.writestr(MANIFEST, json.dumps(manifest))
    return {"files": len(rows), "bytes": sum(r["s"] for r in rows), "skipped": skipped, "manifest": manifest}


# ---------------------------------------------------------------- the backup folder

def list_backups(dest):
    """-> [Path] of this tool's finished backups in dest, newest first (by the time in the name, then the counter)."""
    found = []
    try:
        names = os.listdir(dest)
    except OSError:
        return []
    for n in names:
        m = NAME_RE.match(n)
        if m and (Path(dest) / n).is_file():
            found.append((m.group(1), int(m.group(2) or 0), n))
    found.sort(reverse=True)
    return [Path(dest) / n for _, _, n in found]


def new_name(dest, now, encrypted):
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(now))
    suffix = ".zip.enc" if encrypted else ".zip"
    name, k = PREFIX + stamp + suffix, 1
    while (Path(dest) / name).exists() or (Path(dest) / (name + ".partial")).exists():
        name, k = "%s%s-%d%s" % (PREFIX, stamp, k, suffix), k + 1
    return name


def prune(dest, keep, protect=None, dry_run=True):
    """The backups beyond the newest `keep`. With dry_run=False they are deleted. -> [names]. Only files matching NAME_RE are ever candidates; `protect` is never one."""
    if keep < 1:
        return []
    old = [p for p in list_backups(dest)[keep:] if protect is None or p.name != Path(protect).name]
    if not dry_run:
        for p in old:
            try:
                p.unlink()
            except OSError:
                pass
    return [p.name for p in old]


def make_backup(dest, sources, excludes=DEFAULT_EXCLUDES, passphrase=None, keep=0, dry_run=False, now=None, skip_paths=()):
    """Create one backup. -> result dict {"archive", "files", "bytes", "skipped", "pruned", "encrypted", "dry_run"}. Raises BackupError / OSError on failure; leaves no partial file behind."""
    now = time.time() if now is None else now
    dest = Path(dest)
    skip = list(skip_paths) + [dest]
    encrypted = passphrase is not None
    if dry_run:
        files = bytes_ = 0
        for _arc, path in iter_entries(sources, excludes, skip):
            files += 1
            try:
                bytes_ += path.stat().st_size
            except OSError:
                pass
        return {"archive": str(dest / new_name(dest, now, encrypted)) if dest.exists() else str(dest / (PREFIX + "<time>.zip")), "files": files, "bytes": bytes_,
                "skipped": [], "pruned": prune(dest, keep, dry_run=True) if dest.exists() else [], "encrypted": encrypted, "dry_run": True}
    dest.mkdir(parents=True, exist_ok=True)
    name = new_name(dest, now, encrypted)
    final, partial = dest / name, dest / (name + ".partial")
    try:
        with open(partial, "wb") as f:
            sink = EncryptWriter(f, passphrase) if encrypted else f
            result = write_zip(sink, iter_entries(sources, excludes, skip), now)
            if encrypted:
                sink.close()
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        if result["files"] < 1:
            raise BackupError("nothing was archived (no readable files in the sources)")
        if not encrypted:
            with zipfile.ZipFile(partial) as check:          # cheap look: the archive opens and the manifest is in it
                if MANIFEST not in check.namelist():
                    raise BackupError("the new archive is missing its manifest")
        os.replace(partial, final)
    finally:
        try:
            partial.unlink()
        except OSError:
            pass
    pruned = prune(dest, keep, protect=final, dry_run=False) if keep >= 1 else []
    return {"archive": str(final), "files": result["files"], "bytes": result["bytes"], "skipped": result["skipped"], "pruned": pruned, "encrypted": encrypted, "dry_run": False}


# ---------------------------------------------------------------- command line

def run(args, now=None):
    """-> exit code. Everything that can go wrong becomes a printed line plus an alert, never a traceback."""
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        print("backup_vault: off (%s exists)" % OFF_NAME)
        return 0
    try:
        if args.decrypt:
            passphrase = get_passphrase(args.passphrase_env, prompt=args.prompt_passphrase)
            if not passphrase:
                raise BackupError("no passphrase: set the environment variable %s (or use --prompt-passphrase in a terminal)" % (args.passphrase_env or PASS_ENV))
            if not args.out:
                raise BackupError("--decrypt needs --out FILE")
            decrypt_file(args.decrypt, args.out, passphrase)
            print("decrypted to %s" % args.out)
            return 0
        if args.keep is not None and args.keep < 1:
            raise BackupError("--keep must be 1 or more")
        passphrase = None
        if args.encrypt:
            passphrase = get_passphrase(args.passphrase_env, prompt=args.prompt_passphrase, confirm=True)
            if not passphrase:
                raise BackupError("--encrypt was given but no passphrase was found: set the environment variable %s (or use --prompt-passphrase in a terminal). Nothing was backed up."
                                  % (args.passphrase_env or PASS_ENV))
            check_passphrase(passphrase)
        vault, root = vault_dir(), root_dir()
        dest = backup_dir(args.dest)
        if is_within(dest, vault):
            raise BackupError("the backup folder %s is inside the vault: a backup there protects nothing. Pick another folder with --dest or HARNESS_BACKUP_DIR" % dest)
        if not Path(vault).is_dir():
            print("backup_vault: no vault folder at %s, skipped (nothing to back up, and no old backup is rotated out)" % vault)
            return 0
        sources, notes = build_sources(vault, root, args.include or (), not args.no_scripts, not args.no_boot_file)
        for n in notes:
            print("note: " + n)
        excludes = tuple(DEFAULT_EXCLUDES) + tuple(args.exclude or ())
        res = make_backup(dest, sources, excludes, passphrase, args.keep or 0, args.dry_run, now)
    except BackupError as e:
        return _fail(str(e), args)
    except OSError as e:
        return _fail("%s: %s" % (type(e).__name__, e), args)
    except Exception as e:
        return _fail("unexpected %s: %s" % (type(e).__name__, e), args)
    if res["dry_run"]:
        print("DRY RUN, nothing written. Would create %s with %d file(s), %.1f MB%s" % (res["archive"], res["files"], res["bytes"] / 1e6, ", encrypted" if res["encrypted"] else ""))
        for n in res["pruned"]:
            print("DRY RUN would delete old backup %s" % n)
        return 0
    print("Backup created: %s (%d files, %.1f MB%s)" % (res["archive"], res["files"], res["bytes"] / 1e6, ", encrypted" if res["encrypted"] else ""))
    for n in res["pruned"]:
        print("Deleted old backup %s (keeping the newest %d)" % (n, args.keep))
    if res["skipped"]:
        print("Skipped %d unreadable file(s): %s" % (len(res["skipped"]), "; ".join(res["skipped"][:5])))
        alert("backup made but %d file(s) could not be read: %s" % (len(res["skipped"]), "; ".join(res["skipped"][:3])), dedupe="backup_vault_skipped")
    stamp = time.time() if now is None else now
    try:
        save_json(logs / STATE_NAME, {"last_ok": stamp, "archive": Path(res["archive"]).name, "dest": str(backup_dir(args.dest)), "files": res["files"], "bytes": res["bytes"], "encrypted": res["encrypted"]})
    except OSError:
        pass
    return 0


def _fail(text, args):
    print("backup_vault: FAILED: " + text)
    if not args.dry_run:
        alert("backup failed: " + text, dedupe="backup_vault_failed", kind="backup_failed")
    return 0 if args.job else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="a dated zip of the vault, outside the vault")
    ap.add_argument("--dest", help="backup folder (default: HARNESS_BACKUP_DIR, else ~/assistant-backups)")
    ap.add_argument("--keep", type=int, default=None, help="afterwards delete all but the newest N backups made by this tool (without it nothing is deleted)")
    ap.add_argument("--include", action="append", help="an extra file or folder to include (repeatable)")
    ap.add_argument("--exclude", action="append", help="an extra file or folder name pattern to leave out (repeatable)")
    ap.add_argument("--no-scripts", action="store_true", help="leave out the scripts folder")
    ap.add_argument("--no-boot-file", action="store_true", help="leave out CLAUDE.md")
    ap.add_argument("--encrypt", action="store_true", help="encrypt the archive (passphrase from the environment or a prompt, never the command line)")
    ap.add_argument("--passphrase-env", default=PASS_ENV, help="NAME of the environment variable holding the passphrase (default %s)" % PASS_ENV)
    ap.add_argument("--prompt-passphrase", action="store_true", help="ask for the passphrase in the terminal when the environment variable is empty")
    ap.add_argument("--decrypt", metavar="FILE", help="decrypt an encrypted backup to a plain zip (needs --out)")
    ap.add_argument("--out", help="where --decrypt writes the plain zip")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen and write nothing")
    ap.add_argument("--job", action="store_true", help="scheduled run: always exit 0 (problems are alerts)")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
