"""A recorded session: the washer id, three login replies and the labelled 201 frames.

On disk (one JSON file per session, mode 0600):
  {"id", "wid", "recorded_at", "state", "login": {"101": hex, ...}, "frames": {label: hex}}
"id" is the first 16 hex digits of SHA-256 over the first 32 bytes of the
key sent in the 102 reply. It only names the session; nothing is decrypted.
"""
import base64
import hashlib
import json
import os
import tempfile

from . import wire

LOGIN_IDS = ("101", "102", "103")


class SessionError(Exception):
    pass


def session_id(key_b64):
    return hashlib.sha256(base64.b64decode(key_b64)[:32]).hexdigest()[:16]


def is_id(sid):
    return isinstance(sid, str) and bool(sid) and all(c in "0123456789abcdef" for c in sid)


def id_from_102(raw):
    return session_id(wire.parse(raw).body["key"])


def write_private(path, data):
    """Atomic JSON write: temp file 0600 in the same folder, fsync, rename."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Session:
    def __init__(self, data):
        self.data = data

    @property
    def id(self):
        return self.data["id"]

    @property
    def wid(self):
        return self.data["wid"]

    @property
    def labels(self):
        return sorted(self.data["frames"])

    def login_reply(self, msg_id):
        return bytes.fromhex(self.data["login"][str(msg_id)])

    def frame(self, label):
        raw = self.data["frames"].get(label)
        return bytes.fromhex(raw) if raw else None

    @classmethod
    def load(cls, path):
        try:
            with open(path) as fh:
                data = json.load(fh)
            # the file may come from the shared import folder: check every type that is used later
            if not (is_id(data["id"]) and isinstance(data["wid"], str) and data["wid"]):
                raise ValueError("bad id or wid")
            for msg_id in LOGIN_IDS:
                bytes.fromhex(data["login"][msg_id])
            for label, raw in data["frames"].items():
                if not isinstance(label, str):
                    raise ValueError("bad label")
                if raw:  # an empty entry reads as "not captured", as in frame()
                    bytes.fromhex(raw)
        except FileNotFoundError:
            raise SessionError(f"session file missing: {os.path.basename(path)}") from None
        except (ValueError, KeyError, TypeError, AttributeError, OSError, RecursionError) as exc:
            raise SessionError(
                f"session file {os.path.basename(path)} is unreadable ({type(exc).__name__})") from None
        return cls(data)


class DataDir:
    """The add-on's /data: sessions/, the 'active' pointer, capture markers."""

    def __init__(self, root):
        self.root = root
        self.sessions = os.path.join(root, "sessions")
        os.makedirs(self.sessions, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)

    def path(self, sid):
        if not is_id(sid):
            raise SessionError("bad session id")
        return os.path.join(self.sessions, f"{sid}.json")

    def save(self, session):
        write_private(self.path(session.id), session.data)

    def active_id(self, override=""):
        if override:
            # the log shows only the first characters of an id: accept a unique prefix
            names = [n[:-5] for n in os.listdir(self.sessions) if n.endswith(".json")]
            found = [n for n in names if n.startswith(override.lower())]
            return found[0] if len(found) == 1 else override
        try:
            with open(os.path.join(self.root, "active")) as fh:
                return fh.read().strip()
        except FileNotFoundError:
            return ""

    def load_active(self, override=""):
        sid = self.active_id(override)
        return Session.load(self.path(sid)) if sid else None

    def adopt_import(self, src):
        """Move an imported session file in; make it active if none is.

        The source sits on a shared folder other add-ons can read, so it is
        deleted once copied. Returns the session id, or None if no file.
        """
        if not os.path.exists(src):
            return None
        try:
            session = Session.load(src)
            self.save(session)
            self.activate_if_none(session.id)
            return session.id
        finally:
            os.unlink(src)

    def activate_if_none(self, sid):
        if self.active_id():
            return False
        tmp = os.path.join(self.root, ".active")
        with open(tmp, "w") as fh:
            fh.write(sid)
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(self.root, "active"))
        return True

    def marker(self, name):
        return os.path.join(self.root, name)


def import_capture_files(login_path, commands_path, library_path):
    """Build a session from the capture tool's three files.

    Keeps a frame only when a library label points to it, it is tagged with
    the library's session, and it is a 201 whose seq matches the pointer.
    Returns (Session, report).
    """
    with open(login_path) as fh:
        login = json.load(fh)
    with open(commands_path) as fh:
        items = json.load(fh)["items"]
    with open(library_path) as fh:
        library = json.load(fh)

    replies = {k: login["replies"][k] for k in LOGIN_IDS}
    sid = id_from_102(bytes.fromhex(replies["102"]))
    if sid != library["key"]:
        raise SessionError("login replies and library belong to different sessions")

    unsupported = set(library.get("unsupported_options", ()))
    frames, missing, foreign, mismatched, skipped = {}, [], [], [], []
    for name, entry in library["commands"].items():
        if entry["prop"] in unsupported:
            skipped.append(name)
            continue
        pointer = entry["cmd"]
        item = items.get(pointer)
        if item is None:
            missing.append(name)
            continue
        if library["key"] not in item.values():
            foreign.append(name)
            continue
        raw = bytes.fromhex(item["raw"])
        frame = wire.parse(raw)
        if frame.msg_id != 201 or str(frame.body.get("seq")) != pointer.rsplit("_", 1)[-1]:
            mismatched.append(name)
            continue
        frames[f"{entry['prop']}={entry['val']}"] = raw.hex()

    session = Session({
        "id": sid,
        "wid": login["wid"],
        "recorded_at": login.get("recorded_at"),
        "state": "ready",
        "login": replies,
        "frames": frames,
    })
    report = {
        "session": sid,
        "frames_in_file": len(items),
        "frames_of_session": sum(1 for it in items.values() if library["key"] in it.values()),
        "library_labels": len(library["commands"]),
        "imported": len(frames),
        "missing": missing,
        "other_session": foreign,
        "not_a_matching_201": mismatched,
        "unsupported": skipped,
    }
    return session, report
