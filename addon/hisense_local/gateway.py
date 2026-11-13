"""The TLS endpoint the washer logs in to. Local mode replays a recorded session.

This module never opens an outbound connection. Capture mode lives in
capture.py and is handed in only when the owner arms it.
"""
import asyncio
import logging
import ssl

from . import wire

log = logging.getLogger("gateway")

READ_ERRORS = (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
               ConnectionError, OSError, ValueError)


def server_context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def short(sid):
    return f"{sid[:4]}…" if sid else "none"


class Gateway:
    def __init__(self, session, *, peer_ip="", login_timeout=15.0, ack_timeout=5.0,
                 idle_timeout=540.0, max_half_open=4, capture=None,
                 on_status=None, on_link=None):
        self.session = session
        self.peer_ip = peer_ip
        self.login_timeout = login_timeout
        self.ack_timeout = ack_timeout
        self.idle_timeout = idle_timeout
        self.max_half_open = max_half_open
        self.capture = capture
        self.on_status = on_status or (lambda values: None)
        self.on_link = on_link or (lambda ready: None)
        self.link = None  # writer of the logged-in washer connection
        self.ready = False
        self.half_open = 0
        self.lock = asyncio.Lock()
        self.awaiting = {}  # seq -> future of the ack body

    async def serve(self, port, ssl_ctx, host=None):
        return await asyncio.start_server(self.handle, host, port, ssl=ssl_ctx, limit=wire.MAX_LINE)

    # -- connections ---------------------------------------------------------------
    async def handle(self, reader, writer):
        try:
            await self._handle(reader, writer)
        except Exception as exc:  # whatever a peer sends, the socket is closed and nothing of it is logged
            log.warning("connection dropped (%s)", type(exc).__name__)
        finally:
            await self._close(writer)

    async def _handle(self, reader, writer):
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        if self.peer_ip and peer != self.peer_ip:
            log.warning("refused connection from %s (not the configured peer)", peer)
            return await self._close(writer)
        first_capture = self.session is None and self.capture is not None and self.capture.armed()
        if first_capture and not self.peer_ip:
            # with no session there is no washer id to check: only peer_ip says who may log in
            log.warning("first capture needs peer_ip: closing")
            first_capture = False
        if self.session is None and not first_capture:
            log.warning("washer connected but no session is active: closing")
            return await self._close(writer)
        if self.half_open >= self.max_half_open:
            log.warning("too many connections waiting for login: closing")
            return await self._close(writer)

        self.half_open += 1
        try:
            hello = await self._wait_hello(reader)
        finally:
            self.half_open -= 1
        if hello is None:
            return await self._close(writer)
        wid = hello.body.get("wid")
        if self.session is not None and wid != self.session.wid:
            log.warning("login with an unknown washer id: closing")
            return await self._close(writer)
        if not (isinstance(wid, str) and wid):  # first capture: must not spend the day's attempt
            log.warning("login without a washer id: closing")
            return await self._close(writer)

        if self.capture is not None:
            if self.capture.running:
                await self.capture.stop("washer reconnected")
            elif self.capture.armed():
                self.drop_link()
                return await self.capture.relay(self, hello, reader, writer)
        if self.session is None:  # the armed day's one capture was used meanwhile
            return await self._close(writer)
        await self._local(reader, writer)

    async def _wait_hello(self, reader):
        """Read until a 101 arrives. Other frames are ignored, nothing is answered."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.login_timeout
        while True:
            try:
                raw = await asyncio.wait_for(reader.readuntil(wire.TERMINATOR),
                                             max(0.0, deadline - loop.time()))
            except READ_ERRORS as exc:
                log.info("no login (%s)", type(exc).__name__)
                return None
            try:
                frame = wire.parse(raw)
            except wire.BadFrame:
                continue
            if frame.msg_id == 101:
                return frame
            log.info("ignored id=%s before login", frame.msg_id)

    async def _local(self, reader, writer):
        self.drop_link()
        self.link, self.ready = writer, False
        self.on_link(False)
        log.info("washer logging in, session %s", short(self.session.id))
        writer.write(self.session.login_reply(101))
        try:
            while True:
                timeout = self.idle_timeout if self.ready else self.login_timeout
                raw = await asyncio.wait_for(reader.readuntil(wire.TERMINATOR), timeout)
                try:
                    frame = wire.parse(raw)
                except wire.BadFrame as exc:
                    log.info("unreadable frame (%s), len=%d", exc, len(raw))
                    continue
                if frame.msg_id in (102, 103):
                    writer.write(self.session.login_reply(frame.msg_id))
                    if frame.msg_id == 103 and not self.ready:
                        self.ready = True
                        log.info("washer ready")
                        self.on_link(True)
                elif frame.msg_id == 902:
                    writer.write(wire.HEARTBEAT)
                else:
                    self.washer_frame(frame)
                await writer.drain()
        except READ_ERRORS as exc:
            log.info("washer link closed (%s)", type(exc).__name__)
        finally:
            if self.link is writer:
                self.link, self.ready = None, False
                self._release_waiters()
                self.on_link(False)
            await self._close(writer)

    def washer_frame(self, frame):
        """Status and acks, shared with capture mode."""
        if frame.msg_id in (901, 201):
            values = wire.status_values(frame.body)
            if values is not None:
                self.on_status(values)
            if frame.msg_id == 201:
                seq = frame.body.get("seq")
                if not isinstance(seq, (int, str)):
                    return
                log.info("ack seq=%.20s err=%.20s", seq, frame.body.get("err"))
                waiter = self.awaiting.pop(seq, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(frame.body)
        else:
            log.info("no answer to id=%s len=%d", frame.msg_id, len(frame.raw))

    def drop_link(self):
        if self.link is not None:
            self.link.close()
            self.link, self.ready = None, False
            self._release_waiters()

    def _release_waiters(self):
        for waiter in self.awaiting.values():
            if not waiter.done():
                waiter.set_result(None)
        self.awaiting.clear()

    @staticmethod
    async def _close(writer):
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError, ssl.SSLError):
            pass

    # -- commands ------------------------------------------------------------------
    async def send(self, label):
        return (await self.exchange(label))[0]

    async def exchange(self, label):
        """Replay one recorded frame and wait for its ack. Returns (result, status values)."""
        if self.capture is not None and self.capture.running:
            return "capture running", None
        raw = self.session.frame(label) if self.session else None
        if raw is None:
            return "not captured", None
        async with self.lock:
            if self.link is None:
                return "washer offline", None
            if not self.ready:
                return "logging in", None
            seq = wire.seq_of(raw)
            waiter = asyncio.get_running_loop().create_future()
            self.awaiting[seq] = waiter
            try:
                self.link.write(raw)
                await self.link.drain()
                body = await asyncio.wait_for(waiter, self.ack_timeout)
            except asyncio.TimeoutError:
                return "no ack", None
            except (ConnectionError, OSError):
                return "washer offline", None
            finally:
                self.awaiting.pop(seq, None)
        if body is None:
            return "washer offline", None
        err = body.get("err")
        return ("ok" if err == 0 else f"refused err={err}"), wire.status_values(body)
