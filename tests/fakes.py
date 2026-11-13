"""Synthetic material for tests. Everything key-shaped is made at run time."""
import asyncio
import base64
import json
import os

from hisense_local import session as sessionlib
from hisense_local import wire


def line(body, signed=True):
    sig = os.urandom(32).hex().encode() if signed else b""
    return b"HS" + json.dumps(body, separators=(",", ":")).encode() + sig + b"\r\n"


def uart(values, checksum=None):
    """Encode values into an F4 F5 ... F4 FB block (inverse of wire.uart_values)."""
    data = bytes(((v + 1) & 0xFF) ^ 0xFF for v in values)
    check = (sum(data) & 0xFFFF) if checksum is None else checksum
    inner = (data + check.to_bytes(2, "big")).replace(b"\xf4", b"\xf4\xf4")
    return b"\xf4\xf5" + inner + b"\xf4\xfb"


def status_body(msg_id, values, token, **extra):
    body = {"id": msg_id, "token": token, "err": 0, **extra, "status": [
        {"msgtype": 30, "msgsubtype": 0, "result": base64.b64encode(b"\x80\x01\x00\x02").decode()},
        {"msgtype": 102, "msgsubtype": 0, "result": base64.b64encode(uart(values)).decode()},
    ]}
    return body


class FakeSession:
    """A session with random wid/token/key and a few made-up 201 frames."""

    LABELS = ["Actions=2", "Actions=1", "Remote_hold=1", "Remote_hold=0", "DownLight=0",
              "DownLight=1", "Selected_program_ID=9", "Selected_program_ID=11", "temperature=1",
              "temperature=3", "Spin_speed_rpm=8", "Extra_Rinse=1", "Extra_Rinse=0", "Child_lock=0",
              "Child_lock=1", "DelayStartTime=0", "DelayStartTime=3"]

    def __init__(self, labels=None):
        self.wid = os.urandom(18).hex()
        self.token = os.urandom(16).hex()
        self.key = base64.b64encode(os.urandom(48)).decode()
        self.sid = sessionlib.session_id(self.key)
        self.seqs = {}
        self.login = {
            "101": line({"id": 101, "token": self.token, "err": 0}),
            "102": line({"id": 102, "key": self.key, "err": 0}),
            "103": line({"id": 103, "args": [{"hb": 180, "rp": 360, "retry": 60,
                                              "time": 1700000000, "timezone": 0}], "err": 0}),
        }
        self.frames = {}
        for n, label in enumerate(labels or self.LABELS):
            seq = 100000000 + n
            self.seqs[label] = seq
            com = base64.b64encode(os.urandom(112)).decode()
            self.frames[label] = line({"id": 201, "seq": seq, "com": com, "cmdversion": -1})

    def session(self):
        return sessionlib.Session({
            "id": self.sid, "wid": self.wid, "recorded_at": 1700000000.0, "state": "ready",
            "login": {k: v.hex() for k, v in self.login.items()},
            "frames": {k: v.hex() for k, v in self.frames.items()},
        })

    # what the washer sends
    def hello(self, wid=None):
        return line({"id": 101, "wid": wid or self.wid, "ver": "WF.00.00.00.00", "ProtVer": "1"})

    def asks(self, msg_id):
        return line({"id": msg_id, "token": self.token})

    def ack(self, seq, err=0, values=None):
        return line(status_body(201, values or [0] * 20, self.token, seq=seq, err=err))

    def status(self, values):
        return line(status_body(901, values, self.token))

    def write_old_format(self, folder, extra_foreign=0):
        """Write login/commands/library files in the capture tool's layout."""
        other_key = os.urandom(8).hex()
        items, commands = {}, {}
        for label, raw in self.frames.items():
            seq = self.seqs[label]
            pointer = f"cap_{seq}"
            items[pointer] = {"raw": raw.hex(), "seq": seq, "at": 0.0, "tag": self.sid}
            prop, val = label.split("=")
            name = {"Actions=2": "START", "Actions=1": "STOP", "Remote_hold=1": "PAUSE",
                    "Remote_hold=0": "RESUME (Remote_hold=0)"}.get(label, label)
            commands[name] = {"cmd": pointer, "com": os.urandom(4).hex(), "prop": prop, "val": int(val)}
        for n in range(extra_foreign):
            seq = 200000000 + n
            items[f"cap_{seq}"] = {"raw": line({"id": 201, "seq": seq, "com": "x"}).hex(),
                                   "seq": seq, "at": 0.0, "tag": other_key}
        if extra_foreign:  # a library label pointing at another session's frame
            commands["Steam=1"] = {"cmd": f"cap_{200000000}", "com": "0", "prop": "Steam", "val": 1}
        commands["PreSoak=1"] = {"cmd": "cap_1", "com": "0", "prop": "PreSoak", "val": 1}
        paths = {n: os.path.join(folder, n + ".json") for n in ("login", "commands", "library")}
        dump = {
            "login": {"recorded_at": 1700000000.0, "wid": self.wid,
                      "replies": {k: v.hex() for k, v in self.login.items()}},
            "commands": {"items": items},
            "library": {"key": self.sid, "program_names": {}, "unsupported_options": ["PreSoak"],
                        "commands": commands},
        }
        for name, path in paths.items():
            with open(path, "w") as fh:
                json.dump(dump[name], fh)
        return paths


class FakeWasher:
    """Client side of the washer link over asyncio streams."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, port, ssl=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ssl)
        return cls(reader, writer)

    async def send(self, raw):
        self.writer.write(raw)
        await self.writer.drain()

    async def read(self, timeout=2.0):
        return await asyncio.wait_for(self.reader.readuntil(wire.TERMINATOR), timeout)

    async def closed(self, timeout=2.0):
        """True when the server closed the link without sending anything more."""
        try:
            data = await asyncio.wait_for(self.reader.read(), timeout)
        except (ConnectionError, OSError):
            return True
        return data == b""

    async def login(self, fake):
        replies = []
        for msg_id, raw in (("101", fake.hello()), ("102", fake.asks(102)), ("103", fake.asks(103))):
            await self.send(raw)
            replies.append(await self.read())
        return replies

    def close(self):
        self.writer.close()
