import asyncio
import datetime
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from hisense_local import capture as capturelib
from hisense_local import gateway as gatewaylib
from hisense_local import session as sessionlib
from hisense_local import wire
from tests import fakes
from tests.fakes import FakeWasher

DAY = datetime.date(2026, 1, 2)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeCloud:
    """Plain-TCP stand-in for the vendor cloud; answers the login with a new key."""

    def __init__(self, wid):
        self.keys = fakes.FakeSession()
        self.keys.wid = wid
        self.connections, self.received, self.writer = 0, [], None

    async def handle(self, reader, writer):
        self.connections += 1
        self.writer = writer
        try:
            while True:
                raw = await reader.readuntil(wire.TERMINATOR)
                self.received.append(raw)
                msg_id = wire.parse(raw).msg_id
                if msg_id in (101, 102, 103):
                    writer.write(self.keys.login[str(msg_id)])
                elif msg_id == 902:
                    writer.write(wire.HEARTBEAT)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def push(self, raw):
        self.writer.write(raw)
        await self.writer.drain()


class CaptureCase(unittest.IsolatedAsyncioTestCase):
    capture_date = DAY.isoformat()

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = sessionlib.DataDir(os.path.join(self.tmp.name, "data"))
        self.fake = fakes.FakeSession()
        self.data.save(self.fake.session())
        self.active_path = self.data.path(self.fake.sid)
        with open(self.active_path, "rb") as fh:
            self.active_bytes = fh.read()
        self.cloud = FakeCloud(self.fake.wid)
        self.cloud_server = await asyncio.start_server(self.cloud.handle, "127.0.0.1", 0)
        self.got = []
        self.capture = capturelib.Capture(
            self.data, self.capture_date, "127.0.0.1", self.cloud_server.sockets[0].getsockname()[1],
            upstream_ssl=None, on_got=lambda *a: self.got.append(a), today=lambda: DAY)
        self.gw = gatewaylib.Gateway(self.fake.session(), capture=self.capture, login_timeout=1.0)
        self.server = await self.gw.serve(0, None, host="127.0.0.1")
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients = []

    async def asyncTearDown(self):
        for c in self.clients:
            c.close()
        await self.capture.stop("test end")
        self.gw.drop_link()
        for server in (self.server, self.cloud_server):
            server.close()
            await server.wait_closed()
        self.tmp.cleanup()

    async def washer(self):
        c = await FakeWasher.connect(self.port)
        self.clients.append(c)
        return c

    async def settle(self):
        for _ in range(20):
            await asyncio.sleep(0.01)

    def active_untouched(self):
        with open(self.active_path, "rb") as fh:
            return fh.read() == self.active_bytes

    def recorded(self):
        return sessionlib.Session.load(self.data.path(self.cloud.keys.sid)).data


class CaptureTest(CaptureCase):
    async def logged_in(self):
        c = await self.washer()
        replies = await c.login(self.fake)
        return c, replies

    async def test_relay_is_byte_exact_and_records(self):
        c = await self.washer()
        sent = [self.fake.hello(), self.fake.asks(102), self.fake.asks(103)]
        replies = []
        for raw in sent:
            await c.send(raw)
            replies.append(await c.read())
        self.assertEqual(replies, [self.cloud.keys.login[k] for k in ("101", "102", "103")])
        self.assertEqual(self.cloud.received[:3], sent)
        self.assertTrue(self.capture.running)
        self.assertEqual(await self.gw.send("Actions=2"), "capture running")

        self.capture.next_label = "Steam=1"
        frame = fakes.line({"id": 201, "seq": 4242, "com": "c29tZQ==", "cmdversion": -1})
        await self.cloud.push(frame)
        self.assertEqual(await c.read(), frame)
        ack = self.fake.ack(4242)
        await c.send(ack)
        await self.settle()
        self.assertEqual(self.cloud.received[-1], ack)
        await self.cloud.push(fakes.line({"id": 201, "seq": 77, "com": "eA==", "cmdversion": -1}))
        await c.read()
        await self.settle()

        rec = self.recorded()
        self.assertEqual(rec["frames"]["Steam=1"], frame.hex())
        self.assertIn("unlabelled_77", rec["frames"])
        self.assertEqual(rec["login"]["102"], self.cloud.keys.login["102"].hex())
        self.assertEqual(self.got[0], ("Steam=1", 4242, self.cloud.keys.sid))
        self.assertTrue(self.active_untouched())

    async def test_safe_303_passes(self):
        c, _ = await self.logged_in()
        ok = fakes.line({"id": 303, "update_flag": 0, "url": "", "md5": "", "err": 0})
        await self.cloud.push(ok)
        self.assertEqual(await c.read(), ok)

    async def test_firmware_offer_blocked_and_capture_ends(self):
        c, _ = await self.logged_in()
        await self.cloud.push(fakes.line({"id": 303, "update_flag": 1, "url": "http://192.0.2.9/fw"}))
        self.assertTrue(await c.closed())
        await self.settle()
        self.assertFalse(self.capture.running)
        self.assertEqual(self.recorded()["state"], "closed")

    async def test_disguised_firmware_offer_blocked(self):
        # the same key twice: Python keeps the last one, the washer may keep the first
        for raw in (b'HS{"id":303,"update_flag":1,"url":"http://192.0.2.9/fw","id":902}\r\n',
                    b'HS{"id":303,"update_flag":1,"url":"x","update_flag":0,"url":""}\r\n',
                    b'HS{"id":"902","update_flag":1,"url":"x"}\r\n',
                    b'HS{"id":303,"update_flag":false}\r\n'):
            self.assertFalse(capturelib.firmware_safe(wire.parse(raw)), raw)
        c, _ = await self.logged_in()
        await self.cloud.push(b'HS{"id":303,"update_flag":1,"url":"http://192.0.2.9/fw","id":902}\r\n')
        self.assertTrue(await c.closed())

    def test_host_name_as_cloud_address_disarms(self):
        with self.assertLogs("capture", "ERROR"):
            cap = capturelib.Capture(self.data, self.capture_date, "cloud.example", today=lambda: DAY)
        self.assertFalse(cap.armed())

    async def test_second_key_ends_capture(self):
        c, _ = await self.logged_in()
        other = fakes.FakeSession()
        await self.cloud.push(other.login["102"])
        self.assertTrue(await c.closed())
        await self.settle()
        self.assertFalse(self.capture.running)
        self.assertEqual(self.recorded()["state"], "closed")
        self.assertFalse(os.path.exists(self.data.path(other.sid)))
        self.assertTrue(self.active_untouched())

    async def test_reconnect_ends_capture_and_goes_local(self):
        first, _ = await self.logged_in()
        second = await self.washer()
        replies = await second.login(self.fake)
        self.assertEqual(replies[0], self.fake.login["101"])  # the active session, not the cloud
        self.assertTrue(await first.closed())
        await self.settle()
        self.assertFalse(self.capture.running)
        self.assertEqual(self.cloud.connections, 1)
        self.assertTrue(self.active_untouched())

    async def test_wrong_wid_never_reaches_cloud(self):
        c = await self.washer()
        await c.send(self.fake.hello(wid="1" * 36))
        self.assertTrue(await c.closed())
        self.assertEqual(self.cloud.connections, 0)
        self.assertTrue(self.capture.armed())


class NotArmedTest(CaptureCase):
    capture_date = "2026-01-01"

    async def test_cloud_sees_no_connection(self):
        self.assertFalse(self.capture.armed())
        c = await self.washer()
        replies = await c.login(self.fake)
        self.assertEqual(replies[0], self.fake.login["101"])
        self.assertEqual(self.cloud.connections, 0)

    async def test_marker_disarms_same_day(self):
        self.capture.capture_date = DAY.isoformat()
        with open(self.capture.marker(DAY.isoformat()), "w") as fh:
            fh.write("used")
        self.assertFalse(self.capture.armed())
        c = await self.washer()
        await c.login(self.fake)
        self.assertEqual(self.cloud.connections, 0)


class LocalModeImportTest(unittest.TestCase):
    def test_local_mode_never_loads_capture(self):
        code = textwrap.dedent("""
            import asyncio, sys
            from tests import fakes
            from hisense_local import app, bridge, gateway
            async def main():
                fake = fakes.FakeSession()
                gw = gateway.Gateway(fake.session())
                server = await gw.serve(0, None, host="127.0.0.1")
                c = await fakes.FakeWasher.connect(server.sockets[0].getsockname()[1])
                await c.login(fake)
                c.close()
                server.close()
            asyncio.run(main())
            print(json.dumps("hisense_local.capture" in sys.modules))
        """).replace("import asyncio, sys", "import asyncio, json, sys")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                             timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]), False)


if __name__ == "__main__":
    unittest.main()


class FirstCaptureTest(CaptureCase):
    """A new user has no session yet: the armed day's capture must still record one."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.gw.session = None
        self.gw.peer_ip = "127.0.0.1"

    async def test_needs_peer_ip(self):
        self.gw.peer_ip = ""
        c = await self.washer()
        self.assertTrue(await c.closed())
        self.assertEqual(self.cloud.connections, 0)
        self.assertTrue(self.capture.armed())  # the day's attempt is not spent

    async def test_records_without_an_active_session(self):
        c = await self.washer()
        replies = await c.login(self.fake)
        self.assertEqual(replies[0], self.cloud.keys.login["101"])  # answered by the cloud
        for _ in range(100):
            if not self.capture.running and os.path.exists(self.data.path(self.cloud.keys.sid)):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.recorded()["login"]["102"], self.cloud.keys.login["102"].hex())
        c.close()
        for _ in range(100):
            if self.data.active_id():
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.data.active_id(), self.cloud.keys.sid)  # first capture becomes active

    async def test_turned_away_once_the_capture_is_used(self):
        with open(self.capture.marker(DAY.isoformat()), "w") as fh:
            fh.write("used")
        c = await self.washer()
        await c.send(self.fake.hello())
        self.assertTrue(await c.closed())
        self.assertEqual(self.cloud.connections, 0)

    async def test_hello_without_washer_id_does_not_spend_the_capture(self):
        for body in ({"id": 101}, {"id": 101, "wid": 7}, {"id": 101, "wid": ""}):
            c = await self.washer()
            await c.send(fakes.line(body))
            self.assertTrue(await c.closed())
        self.assertEqual(self.cloud.connections, 0)
        self.assertTrue(self.capture.armed())
        self.assertFalse(self.capture.running)

    async def test_incomplete_login_is_recorded_but_not_made_active(self):
        c = await self.washer()
        for raw in (self.fake.hello(), self.fake.asks(102)):  # the link drops before 103
            await c.send(raw)
            await c.read()
        c.close()
        for _ in range(100):
            if not self.capture.running:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.capture.running)
        self.assertTrue(os.path.exists(self.data.path(self.cloud.keys.sid)))
        self.assertEqual(self.data.active_id(), "")
