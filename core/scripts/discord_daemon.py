# -*- coding: utf-8 -*-
"""Discord bridge daemon: an OPTIONAL chat channel between you and your assistant. Nothing in the kit needs it; it does nothing until you set it up.

What it does: one long-lived connection to a Discord bot that YOU create. A message the owner sends to the bot is appended to logs/discord_incoming.jsonl (a watcher, queue_check.py, wakes the assistant);
anything the assistant queues with discord_queue_send.py (a JSON file in logs/discord_outbox/) is sent, split into parts of at most 2000 characters. discord_typing.py shows "typing..." while a reply is prepared.

SETUP (once; none of it can be done for you, and none of it was verified against the real Discord service when this was written):
 1. In the Discord developer portal create an application with a bot. Under Bot turn on "Message Content Intent". Invite the bot to a server you share with yourself, or just open a DM with it.
 2. pip install discord.py   (optional package; the kit works without it)
 3. Give the daemon the bot token WITHOUT putting it in any file of this folder or on a command line: either set the environment variable DISCORD_BOT_TOKEN in your user environment,
    or store it in the operating system's keyring:  python -m keyring set assistant-discord bot_token   (it prompts for the value; needs the optional package `keyring`).
 4. Write logs/discord_config.json. There is NO default owner, you supply your own Discord user id (Discord settings, Advanced, Developer Mode on, then right-click your name, Copy User ID):
        {"owner_id": 123456, "friend_ids": [], "channel_ids": []}
    owner_id    required. The only person whose messages are trusted.
    friend_ids  optional. Other people allowed to DM the bot. Their messages are logged as UNTRUSTED DATA, never as instructions.
    channel_ids optional. Server channels the bot listens in. Messages there are logged too; everyone except the owner is untrusted.
    (friend_ids and channel_ids are re-read on every message; owner_id is read once at start.)
 5. python scripts/discord_daemon.py --check     # prints what it found (never the token) and connects to nothing
 6. python scripts/discord_daemon.py             # run it. Start it at login with your OS's own scheduler if you want it always on.

RULES IT KEEPS
  Trust: only the owner's id is trusted. Every logged message carries "trusted": true|false; text from anyone else is data to read, never an instruction to follow. Messages from other bots and from DMs by
         people not in friend_ids are dropped. Files attached by anyone but the owner are not downloaded.
  Sending: the owner's DM is the default target. A queued message for any other user or channel is sent only when the owner approved that exact text (the kit's logs/approvals.jsonl, written by approve.py);
         the daemon re-checks this itself, so a hand-written outbox file cannot skip the gate. A refused file is moved to logs/discord_outbox/rejected/ and one line goes to logs/alerts.txt.
  Token: read from the environment or the keyring only; kept in memory; removed from every line the daemon logs. It is never written to a file.
  One copy: a lock file (logs/locks/discord_daemon.lock, renewed every 20 s, expires 90 s after a crash) keeps a second daemon out.
  Reconnects: if the connection dies it retries after 5 s, doubling to 300 s, and starts again at 5 s after a run longer than a minute. A wrong token stops it instead of retrying.
  A failed send is retried up to 4 times with growing pauses (parts already sent are not sent twice) and then moved to rejected/.
Kill switch: the file logs/discord_daemon_off.txt (the daemon exits at start, or within a second if it is running).
    python discord_daemon.py --health   # for a timed job: alerts (logs/alerts.txt, at most once per 6 h) when a configured daemon has stopped writing its heartbeat; silent when not set up
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure/testable core: split_text, safe_filename, load_token, load_config, Bridge (takes the discord module as an argument), run_forever, health."""
import asyncio
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import discord_queue_send as dqs  # noqa: E402  (same pack: config parsing, the send gate, the attachment check)

ROOT = Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

TOKEN_ENV = "DISCORD_BOT_TOKEN"
KEYRING_SERVICE = "assistant-discord"
KEYRING_USER = "bot_token"

INCOMING_NAME = "discord_incoming.jsonl"
SENT_NAME = "discord_sent.jsonl"
DAEMON_LOG_NAME = "discord_daemon.log"
MEDIA_NAME = "discord_media"
OFF_NAME = "discord_daemon_off.txt"
TYPING_OFF_NAME = "discord_typing_off.txt"
HEALTH_STATE_NAME = "discord_daemon_health.json"
BEAT_NAME = "discord_daemon"

OUTBOX_POLL_SEC = 0.25
TYPING_MAX_SEC = 120
LOCK_NAME = "discord_daemon"
LOCK_TTL = 90
LOCK_RENEW_S = 20
BEAT_EVERY_S = 30
HEALTH_MAX_AGE_S = 180
HEALTH_REALERT_S = 6 * 3600
LOG_MAX_BYTES = 1000000
MAX_OUTBOX_FILE = 1000000
MAX_PER_PASS = 20
MAX_ATTEMPTS = 4
RETRY_BASE_S = 5
KEEP_REJECTED = 20
DISCORD_LIMIT = 2000
MAX_INCOMING_TEXT = 4000

_SECRETS = set()


class ConfigError(Exception):
    pass


# ---------- paths ----------
def logs_dir():
    return Path(LOGS)


# ---------- logging (the token never reaches a line) ----------
def register_secret(value):
    if value and len(str(value)) >= 8:
        _SECRETS.add(str(value))


def redact(text):
    out = str(text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        out = out.replace(secret, "[token removed]")
    return out


def log(msg):
    """One timestamped line on stdout and in logs/discord_daemon.log (rotated at 1 MB). Never raises; the token is removed first."""
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), redact(msg))
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        path = logs_dir() / DAEMON_LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def alert(text):
    """One line in logs/alerts.txt through the kit's notify.py. Never raises."""
    try:
        import notify
        notify.alert(redact(text), source="discord_daemon", logs=logs_dir())
    except Exception:
        pass


def append_jsonl(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------- small pure helpers ----------
def split_text(text, limit=DISCORD_LIMIT):
    """Chunks of at most `limit` characters, cut at a newline or space when one is near the end (Discord rejects longer messages)."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks = []
    while len(text) > limit:
        cut = max(text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        chunk = text[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        text = text[cut:].lstrip("\n ")
    if text:
        chunks.append(text)
    return chunks


def safe_filename(name, limit=80):
    """A file name that is safe to create under logs/discord_media/: no folders, no odd characters, never empty or hidden."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip(".")
    return (base[:limit] or "file")


def load_config(logs=None):
    """-> {'owner_id': int, 'friend_ids': set, 'channel_ids': set}. Raises ConfigError (with a message for a newcomer) when there is no usable owner id; a bad entry in a list is skipped."""
    lg = Path(logs) if logs else logs_dir()
    data = dqs.read_config(lg)
    owner = dqs.as_id(data.get("owner_id"))
    if owner is None:
        raise ConfigError("logs/%s needs your own Discord user id, for example {\"owner_id\": 123456} (there is no default; see the top of discord_daemon.py)" % dqs.CONFIG_NAME)
    return {"owner_id": owner, "friend_ids": _id_set(data.get("friend_ids")), "channel_ids": _id_set(data.get("channel_ids"))}


def _id_set(values):
    out = set()
    if isinstance(values, (list, tuple, set)):
        for v in values:
            n = dqs.as_id(v)
            if n is not None:
                out.add(n)
    return out


def load_token(env=None, keyring_get=None):
    """-> (token or None, where it came from). The environment variable first, then the OS keyring item (service assistant-discord, user bot_token). Never reads a file."""
    env = os.environ if env is None else env
    value = (env.get(TOKEN_ENV) or "").strip()
    if value and not re.search(r"\s", value):
        return value, "environment variable %s" % TOKEN_ENV
    try:
        if keyring_get is None:
            import keyring
            keyring_get = keyring.get_password
        value = (keyring_get(KEYRING_SERVICE, KEYRING_USER) or "").strip()
        if value and not re.search(r"\s", value):
            return value, "keyring item %s / %s" % (KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        pass
    return None, None


def import_discord():
    """-> the discord module, or None when the optional package is not installed."""
    try:
        import discord
        return discord
    except ImportError:
        return None


# ---------- one running copy ----------
class Singleton:
    """A lock file with an expiry, through the kit's loop_lock.py. Renewing is the same call as acquiring."""

    def __init__(self, logs=None, name=LOCK_NAME, ttl=LOCK_TTL):
        self.logs = Path(logs) if logs else logs_dir()
        self.name = name
        self.ttl = ttl
        self.holder = "%s-%d" % (name, os.getpid())

    def acquire(self):
        import loop_lock
        return bool(loop_lock.try_acquire(self.name, self.holder, self.ttl, str(self.logs)))

    renew = acquire

    def release(self):
        try:
            import loop_lock
            loop_lock.release_file(self.name, self.holder, str(self.logs))
        except Exception:
            pass


# ---------- the bridge: all the behaviour, with the discord module and the client passed in ----------
class Bridge:
    def __init__(self, discord_mod, client, owner_id, logs=None, typing_keeper=None, singleton=None, sleep=None, clock=None, poll=OUTBOX_POLL_SEC):
        self.discord = discord_mod
        self.client = client
        self.owner_id = int(owner_id)
        self.logs = Path(logs) if logs else logs_dir()
        self.typing_keeper = typing_keeper
        self.singleton = singleton
        self.sleep = sleep or asyncio.sleep
        self.clock = clock or time.time
        self.poll = poll
        self.stop_requested = False
        self._owner_dm = None
        self._retry = {}
        self._last_renew = 0.0
        self._last_beat = 0.0

    # --- paths ---
    @property
    def outbox(self):
        return self.logs / dqs.OUTBOX_NAME

    def _lists(self):
        data = dqs.read_config(self.logs)
        return _id_set(data.get("friend_ids")), _id_set(data.get("channel_ids"))

    # --- typing ---
    def _typing_stop(self, channel_id):
        """Ends the typing indicator in one channel. Called right before a message goes out. Never raises."""
        if self.typing_keeper is None:
            return
        try:
            self.typing_keeper.stop(channel_id)
        except Exception:
            log("typing stop error:\n" + traceback.format_exc())

    # --- incoming ---
    async def _download(self, att):
        """Save one attachment of the owner's. The name is made safe and carries the attachment id, so two files called image.png never overwrite each other."""
        try:
            size = int(getattr(att, "size", 0) or 0)
            if size > dqs.MAX_ATTACH_BYTES:
                log("attachment skipped: %d bytes is over the %d byte limit" % (size, dqs.MAX_ATTACH_BYTES))
                return None
            media = self.logs / MEDIA_NAME
            media.mkdir(parents=True, exist_ok=True)
            dest = media / ("%d_%s_%s" % (int(time.time()), getattr(att, "id", 0), safe_filename(getattr(att, "filename", ""))))
            await att.save(dest)
            return str(dest)
        except Exception:
            log("attachment download error:\n" + traceback.format_exc())
            return None

    async def on_message(self, message):
        """Log a message the bot received. Never raises. Only the owner is trusted; every row says so."""
        try:
            await self._on_message(message)
        except Exception:
            log("incoming handler error:\n" + traceback.format_exc())

    async def _on_message(self, message):
        author = message.author
        me = getattr(self.client, "user", None)
        if me is not None and author.id == me.id:
            return                                       # never react to the bot's own sends
        if getattr(author, "bot", False):
            return                                       # other bots are never a source of requests
        is_dm = isinstance(message.channel, self.discord.DMChannel)
        friends, channels = self._lists()
        if is_dm:
            if author.id != self.owner_id and author.id not in friends:
                log("ignored a DM from a sender who is not the owner or in friend_ids")
                return
        elif message.channel.id not in channels:
            return                                       # not a configured server channel: ignored outright
        trusted = author.id == self.owner_id
        saved, skipped = [], []
        for att in list(getattr(message, "attachments", None) or []):
            if trusted:
                path = await self._download(att)
                if path:
                    saved.append(path)
            else:
                skipped.append(safe_filename(getattr(att, "filename", "")))
        reference = getattr(message, "reference", None)
        row = {
            "id": message.id,
            "date": message.created_at.isoformat(),
            "text": str(message.content or "")[:MAX_INCOMING_TEXT],
            "attachments": saved,
            "reply_to": reference.message_id if reference else None,
            "author_id": author.id,
            "author_name": str(getattr(author, "display_name", "") or "")[:80],
            "source": "guild" if not is_dm else ("dm" if trusted else "dm_other"),
            "channel_id": None if (is_dm and trusted) else message.channel.id,
            "trusted": trusted,
        }
        if skipped:
            row["attachments_not_downloaded"] = skipped
        append_jsonl(self.logs / INCOMING_NAME, row)
        if is_dm and trusted and self.typing_keeper is not None:
            try:
                self.typing_keeper.start(message.channel)
            except Exception:
                log("typing start error:\n" + traceback.format_exc())

    # --- outbox ---
    async def _channel_for(self, kind, target_id):
        if kind == "owner":
            if self._owner_dm is None:
                user = await self.client.fetch_user(self.owner_id)
                self._owner_dm = user.dm_channel or await user.create_dm()
            return self._owner_dm
        if kind == "user":
            user = await self.client.fetch_user(target_id)
            return user.dm_channel or await user.create_dm()
        channel = self.client.get_channel(target_id)
        return channel if channel is not None else await self.client.fetch_channel(target_id)

    def _reject(self, fp, reason):
        """Move an outbox file that must not be sent into rejected/ (the newest 20 are kept) and say why, without repeating its text."""
        log("outbox file rejected: %s" % reason)
        alert("a queued Discord message was not sent: %s" % reason)
        try:
            folder = self.outbox / "rejected"
            folder.mkdir(parents=True, exist_ok=True)
            os.replace(fp, folder / ("%d_%s" % (int(time.time() * 1000), fp.name)))
            kept = sorted(folder.iterdir(), key=lambda p: p.name)
            for old in kept[:-KEEP_REJECTED]:
                old.unlink(missing_ok=True)
        except OSError:
            try:
                fp.unlink(missing_ok=True)
            except OSError:
                pass
        self._retry.pop(fp.name, None)

    async def process_outbox_once(self):
        """One pass over logs/discord_outbox/*.json (oldest first, at most 20). -> the number of files finished (sent, dropped or rejected)."""
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            files = sorted(self.outbox.glob("*.json"))[:MAX_PER_PASS]
        except OSError:
            return 0
        finished = 0
        for fp in files:
            state = self._retry.get(fp.name)
            if state and self.clock() < state["not_before"]:
                continue
            if await self._process_file(fp):
                finished += 1
        return finished

    async def _process_file(self, fp):
        """-> True when the file is finished with (sent, stopped typing or rejected), False when it stays for a retry."""
        try:
            if fp.stat().st_size > MAX_OUTBOX_FILE:
                self._reject(fp, "the file is bigger than %d bytes" % MAX_OUTBOX_FILE)
                return True
            data = json.loads(fp.read_bytes().decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            if fp.exists():
                self._reject(fp, "%s is not a readable JSON object" % fp.name)
            return True

        if data.get("typing_stop"):
            try:
                channel = await self._channel_for("owner", self.owner_id)
                self._typing_stop(channel.id)
            except Exception:
                log("typing stop could not resolve the owner's channel")
            fp.unlink(missing_ok=True)
            return True

        text = data.get("text") or ""
        if not isinstance(text, str):
            self._reject(fp, "text is not a string")
            return True
        reply_to, channel_id, user_id = dqs.as_id(data.get("reply_to")), dqs.as_id(data.get("channel_id")), dqs.as_id(data.get("user_id"))
        if (data.get("channel_id") not in (None, "") and channel_id is None) or (data.get("user_id") not in (None, "") and user_id is None):
            self._reject(fp, "a target id is not a valid number")
            return True
        kind, target = dqs.classify_target(self.owner_id, channel_id, user_id)

        photo_path, digest = None, None
        if data.get("photo"):
            photo_path, problem = dqs.check_attachment(data.get("photo"))
            if problem:
                self._reject(fp, problem)
                return True
            try:
                digest = dqs.file_digest(photo_path)
            except OSError:
                self._reject(fp, "the attached file cannot be read")
                return True
        if not text.strip() and photo_path is None:
            self._reject(fp, "the message is empty")
            return True

        allowed, _sha, _fp, why = dqs.gate_check(kind, target, text, digest, self.logs)
        if not allowed:
            self._reject(fp, "a message to %s %s is not approved by the owner (%s)" % (kind, target, why))
            return True

        state = self._retry.get(fp.name) or {"attempts": 0, "not_before": 0.0, "done": 0}
        try:
            channel = await self._channel_for(kind, target)
            self._typing_stop(channel.id)
            reference = None
            if reply_to:
                reference = self.discord.MessageReference(message_id=reply_to, channel_id=channel.id, fail_if_not_exists=False)
            pieces = [p for p in split_text(text) if p.strip()] if text.strip() else []
            plan = [(p, False) for p in pieces]
            if photo_path is not None:
                plan = plan[:-1] + [(plan[-1][0], True)] if plan else [(None, True)]
            last = None
            for index, (piece, with_file) in enumerate(plan):
                if index < state["done"]:
                    continue
                kwargs = {"content": piece}
                if index == 0 and reference is not None:
                    kwargs["reference"] = reference
                if with_file:
                    kwargs["file"] = self.discord.File(str(photo_path))
                last = await channel.send(**kwargs)
                state["done"] = index + 1
            sent_id = getattr(last, "id", None)
            created = getattr(last, "created_at", None)
            append_jsonl(self.logs / SENT_NAME, {
                "id": sent_id, "date": created.isoformat() if created else None, "text": text,
                "photo": photo_path.name if photo_path else None, "reply_to": reply_to, "to": "%s %s" % (kind, target),
            })
            log("sent %d part(s), %d chars, to %s" % (len(plan), len(text), kind) + (" with a file" if photo_path else "") + (" (reply)" if reply_to else ""))
            fp.unlink(missing_ok=True)
            self._retry.pop(fp.name, None)
            return True
        except Exception as e:
            state["attempts"] += 1
            if state["attempts"] >= MAX_ATTEMPTS:
                self._reject(fp, "sending failed %d times (%s)" % (state["attempts"], type(e).__name__))
                return True
            state["not_before"] = self.clock() + RETRY_BASE_S * (2 ** (state["attempts"] - 1))
            self._retry[fp.name] = state
            log("send failed for %s (%s), attempt %d of %d; will retry" % (fp.name, type(e).__name__, state["attempts"], MAX_ATTEMPTS))
            return False

    # --- the outbox loop ---
    def _beat(self):
        now = self.clock()
        if now - self._last_beat < BEAT_EVERY_S:
            return
        self._last_beat = now
        try:
            import heartbeat
            heartbeat.touch(BEAT_NAME, self.logs / "heartbeats")
        except Exception:
            pass

    def _keep_running(self):
        """False (and stop_requested set) when the kill-switch file appeared or another copy took over the lock."""
        if (self.logs / OFF_NAME).exists():
            log("%s found: shutting down" % OFF_NAME)
            self.stop_requested = True
            return False
        if self.singleton is not None and self.clock() - self._last_renew >= LOCK_RENEW_S:
            self._last_renew = self.clock()
            if not self.singleton.renew():
                log("the lock was taken by another copy: shutting down")
                self.stop_requested = True
                return False
        return True

    async def outbox_loop(self):
        await self.client.wait_until_ready()
        while True:
            try:
                if not self._keep_running():
                    await self.client.close()
                    return
                self._beat()
                await self.process_outbox_once()
            except Exception:
                log("outbox loop error:\n" + traceback.format_exc())
            await self.sleep(self.poll)


# ---------- building the real client ----------
def build_client(discord_mod, cfg, singleton=None):
    """-> (client, bridge). The client class is created here so this module imports without the discord package."""
    intents = discord_mod.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    holder = {}

    class AssistantClient(discord_mod.Client):
        async def setup_hook(self):
            holder["task"] = asyncio.get_running_loop().create_task(holder["bridge"].outbox_loop())

        async def on_ready(self):
            log("daemon started, logged in as %s" % getattr(self.user, "name", "the bot"))

        async def on_message(self, message):
            await holder["bridge"].on_message(message)

    client = AssistantClient(intents=intents)
    keeper = None
    try:
        from discord_typing import TypingKeeper
        keeper = TypingKeeper(off_switch=logs_dir() / TYPING_OFF_NAME, max_seconds=TYPING_MAX_SEC, log=log)
    except Exception:
        keeper = None
    bridge = Bridge(discord_mod, client, cfg["owner_id"], logs=logs_dir(), typing_keeper=keeper, singleton=singleton)
    holder["bridge"] = bridge
    return client, bridge


async def run_forever(discord_mod, token, cfg, singleton=None, sleep=None, build=None):
    """Run the client, reconnecting with a growing pause (5 s doubling to 300 s; back to 5 s after a run longer than a minute)."""
    sleep = sleep or asyncio.sleep
    build = build or build_client
    login_failure = getattr(discord_mod, "LoginFailure", None)
    backoff = 5
    while True:
        if (logs_dir() / OFF_NAME).exists():
            log("%s found: not connecting" % OFF_NAME)
            return
        started = time.time()
        client, bridge = build(discord_mod, cfg, singleton)
        try:
            await client.start(token)
            log("client loop exited cleanly - reconnecting")
        except Exception as e:
            if isinstance(login_failure, type) and isinstance(e, login_failure):
                log("Discord refused the bot token: not retrying. Check the token in %s or the keyring." % TOKEN_ENV)
                alert("the Discord bot token was refused; the daemon stopped. Make a fresh token in the developer portal and store it again.")
                return
            log("client loop crashed, will retry:\n" + traceback.format_exc())
        finally:
            if not client.is_closed():
                await client.close()
        if getattr(bridge, "stop_requested", False):
            return
        if time.time() - started > 60:
            backoff = 5
        await sleep(backoff)
        backoff = min(backoff * 2, 300)


# ---------- health (for a timed job) ----------
def health(logs=None, now=None):
    """-> (exit code 0, one line). Alerts in logs/alerts.txt, at most once per 6 hours, when a configured daemon has not written its heartbeat for 3 minutes. Silent when not set up or switched off."""
    lg = Path(logs) if logs else logs_dir()
    now = time.time() if now is None else now
    if dqs.owner_id(lg) is None:
        return 0, "discord_daemon: not set up (no owner_id in logs/%s); nothing to check" % dqs.CONFIG_NAME
    if (lg / OFF_NAME).exists():
        return 0, "discord_daemon: switched off by logs/%s" % OFF_NAME
    try:
        import heartbeat
        age = heartbeat.age_seconds(BEAT_NAME, lg / "heartbeats", now)
    except Exception:
        age = None
    if age is not None and age <= HEALTH_MAX_AGE_S:
        return 0, "discord_daemon: running (heartbeat %d s old)" % age
    state_path = lg / HEALTH_STATE_NAME
    try:
        last = float(json.loads(state_path.read_text(encoding="utf-8")).get("alerted_at", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        last = 0.0
    note = "no heartbeat" if age is None else "heartbeat %d s old" % age
    if now - last < HEALTH_REALERT_S:
        return 0, "discord_daemon: not running (%s); already reported" % note
    try:
        import notify
        notify.alert("the Discord daemon does not look like it is running (%s). Start it with: python scripts/discord_daemon.py" % note, source="discord_daemon", logs=lg)
        dqs.write_json_atomic(state_path, {"alerted_at": now})
    except Exception:
        pass
    return 0, "discord_daemon: not running (%s); alert written" % note


# ---------- command line ----------
def check_report(env=None, keyring_get=None, discord_mod=None, logs=None):
    """-> [lines]: what the daemon found, without connecting and without ever showing the token."""
    lg = Path(logs) if logs else logs_dir()
    lines = []
    try:
        cfg = load_config(lg)
        lines.append("config: ok (owner id set; %d friend id(s); %d channel id(s))" % (len(cfg["friend_ids"]), len(cfg["channel_ids"])))
    except ConfigError as e:
        lines.append("config: MISSING - %s" % e)
    token, where = load_token(env, keyring_get)
    lines.append("token: found in %s" % where if token else "token: MISSING - set %s in your user environment, or run: python -m keyring set %s %s" % (TOKEN_ENV, KEYRING_SERVICE, KEYRING_USER))
    lines.append("discord.py: installed" if discord_mod is not None else "discord.py: MISSING - run: pip install discord.py")
    lines.append("switched off: %s" % ("yes (logs/%s exists)" % OFF_NAME if (lg / OFF_NAME).exists() else "no"))
    return lines


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--health" in argv:
        code, line = health()
        print(line)
        return code
    if "--check" in argv:
        for line in check_report(discord_mod=import_discord()):
            print(line)
        return 0
    if (logs_dir() / OFF_NAME).exists():
        print("discord_daemon: switched off (logs/%s exists)" % OFF_NAME)
        return 0
    try:
        cfg = load_config()
    except ConfigError as e:
        print("discord_daemon: %s" % e)
        return 2
    discord_mod = import_discord()
    if discord_mod is None:
        print("discord_daemon: the optional package discord.py is not installed. Run: pip install discord.py")
        return 2
    token, _where = load_token()
    if not token:
        print("discord_daemon: no bot token found. Set %s in your user environment, or run: python -m keyring set %s %s" % (TOKEN_ENV, KEYRING_SERVICE, KEYRING_USER))
        return 2
    register_secret(token)
    singleton = Singleton()
    if not singleton.acquire():
        log("another discord_daemon instance already holds the lock - exiting")
        return 0
    try:
        asyncio.run(run_forever(discord_mod, token, cfg, singleton))
    except KeyboardInterrupt:
        pass
    except Exception:
        log("daemon crashed:\n" + traceback.format_exc())
        return 1
    finally:
        singleton.release()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
