"""Capture mode: relay one washer login to the vendor cloud and record a new session.

Imported only when the owner sets capture_date. The active session is never
modified; the recording goes to a new file and is promoted by hand, except a
first capture with a complete login, which becomes active when none is.
"""
import asyncio
import datetime
import ipaddress
import json
import logging
import os
import ssl
import time

from . import session as sessionlib
from . import wire
from .gateway import READ_ERRORS, short

log = logging.getLogger("capture")

PASS_THROUGH = {101, 102, 103, 201, 902}


def upstream_context():
    # The cloud certificate does not match its name and the washer never checks it either.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def firmware_safe(frame):
    """True for a cloud frame the washer may see.

    The washer's parser may not read a frame the way Python does: a repeated key
    (first or last wins?) or an id sent as a string could hide an update order
    behind a harmless-looking one, so such frames are refused.
    """
    try:
        json.loads(frame.raw[2:frame.raw.rfind(b"}") + 1], object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError):
        return False
    if type(frame.body.get("id")) is not int:
        return False
    if frame.msg_id in PASS_THROUGH:
        return True
    flag = frame.body.get("update_flag")
    return frame.msg_id == 303 and type(flag) is int and flag == 0 and not frame.body.get("url")


class Capture:
    def __init__(self, data, capture_date, cloud_addr, cloud_port=443, upstream_ssl="default",
                 on_got=None, today=datetime.date.today, idle_timeout=540.0,
                 learn=False, on_cmd=None, on_close=None):
        self.data = data
        self.capture_date = capture_date
        try:  # an address only: a host name could be answered by anyone, or by this gateway
            ipaddress.ip_address(cloud_addr or "0.0.0.0")
        except ValueError:
            log.error("cloud_addr is not an IP address: capture disabled")
            cloud_addr = ""
        self.cloud_addr = cloud_addr
        self.cloud_port = cloud_port
        self.upstream_ssl = upstream_context() if upstream_ssl == "default" else upstream_ssl
        self.on_got = on_got or (lambda label, seq, sid: None)
        self.learn = learn                                   # continuous relay + passive labelling
        self.on_cmd = on_cmd or (lambda seq: None)           # fed each recorded 201 seq
        self.on_close = on_close or (lambda record: None)    # fed the finished record (learn mode)
        self.today = today
        self.idle_timeout = idle_timeout
        self.running = False
        self.next_label = None
        self.record = None
        self._writers = []
        self._protected = None

    def marker(self, day):
        return self.data.marker(f"capture_used_{day}")

    def armed(self):
        day = self.today().isoformat()
        if not self.cloud_addr:
            return False
        if self.learn:  # relay every login continuously, no one-a-day gate
            return True
        return self.capture_date == day and not os.path.exists(self.marker(day))

    async def stop(self, reason):
        if self.running:
            log.warning("capture ends: %s", reason)
        for writer in self._writers:
            writer.close()

    async def relay(self, gateway, hello, w_reader, w_writer):
        day = self.today().isoformat()
        if not self.learn:  # learn mode relays repeatedly, so it never burns the day's one shot
            with open(self.marker(day), "w") as fh:
                fh.write(str(time.time()))
        self.running, self.next_label = True, None
        self.record = {"id": None, "wid": hello.body["wid"], "recorded_at": time.time(),
                       "state": "open", "login": {}, "frames": {}}
        self._writers = [w_writer]
        self._protected = gateway.session.id if gateway.session else None
        log.warning("capture started: relaying this login to the cloud")
        try:
            c_reader, c_writer = await asyncio.open_connection(
                self.cloud_addr, self.cloud_port, ssl=self.upstream_ssl, limit=wire.MAX_LINE)
        except (OSError, asyncio.TimeoutError) as exc:
            log.error("cloud unreachable (%s)", type(exc).__name__)
            self.running = False
            w_writer.close()
            return
        self._writers.append(c_writer)
        c_writer.write(hello.raw)
        up = asyncio.ensure_future(self._washer_to_cloud(gateway, w_reader, c_writer))
        down = asyncio.ensure_future(self._cloud_to_washer(c_reader, w_writer))
        try:
            await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (up, down):
                task.cancel()
            await asyncio.gather(up, down, return_exceptions=True)
            for writer in self._writers:
                writer.close()
            self.record["state"] = "closed"
            self._save()
            self.running = False
            log.warning("capture closed, session %s, %d frames", short(self.record["id"]),
                        len(self.record["frames"]))
            if self.learn:
                self.on_close(self.record)  # relabel + promote if richer than the active session
            else:
                self._activate()

    def _activate(self):
        """Make a first capture the active session, but only one the next start can load."""
        sid = self.record["id"]
        if not sid or self.data.active_id():
            return
        try:
            sessionlib.Session.load(self.data.path(sid))
        except sessionlib.SessionError:
            log.warning("capture %s has no complete login: not made active", short(sid))
            return
        if self.data.activate_if_none(sid):
            log.warning("no session was active: %s is now active (restart to use it)", short(sid))

    async def _washer_to_cloud(self, gateway, reader, c_writer):
        try:
            while True:
                raw = await asyncio.wait_for(reader.readuntil(wire.TERMINATOR), self.idle_timeout)
                c_writer.write(raw)
                await c_writer.drain()
                try:
                    frame = wire.parse(raw)
                except wire.BadFrame:
                    continue
                if frame.msg_id in (901, 201):
                    gateway.washer_frame(frame)
        except READ_ERRORS as exc:
            log.info("washer side closed (%s)", type(exc).__name__)

    async def _cloud_to_washer(self, reader, w_writer):
        try:
            while True:
                raw = await asyncio.wait_for(reader.readuntil(wire.TERMINATOR), self.idle_timeout)
                try:
                    frame = wire.parse(raw)
                except wire.BadFrame:
                    log.warning("unreadable frame from cloud, len=%d: dropped", len(raw))
                    return
                if not firmware_safe(frame):
                    log.warning("blocked id=%s len=%d from cloud", frame.msg_id, len(raw))
                    return
                if not self._keep(frame):
                    return
                w_writer.write(raw)
                await w_writer.drain()
        except READ_ERRORS as exc:
            log.info("cloud side closed (%s)", type(exc).__name__)

    def _keep(self, frame):
        """Record a cloud frame. False ends the capture (single-session rule)."""
        rec = self.record
        if frame.msg_id == 102:
            sid = sessionlib.id_from_102(frame.raw)
            if rec["id"] and sid != rec["id"]:
                log.warning("cloud sent a second key: one session per capture")
                return False
            if sid == self._protected:
                log.warning("cloud reused the active session key: not recording over it")
                return False
            rec["id"] = sid
        if frame.msg_id in (101, 102, 103):
            rec["login"][str(frame.msg_id)] = frame.raw.hex()
        elif frame.msg_id == 201:
            seq = frame.body.get("seq")
            label = self.next_label or f"unlabelled_{seq}"
            self.next_label = None
            rec["frames"][label] = frame.raw.hex()
            log.info("recorded seq=%s as %s", seq, label)
            self.on_got(label, seq, rec["id"])
            if isinstance(seq, int):
                self.on_cmd(seq)  # learn mode: pair this command with the status it causes
        self._save()
        return True

    def _save(self):
        if self.record and self.record["id"]:
            sessionlib.write_private(self.data.path(self.record["id"]), self.record)
