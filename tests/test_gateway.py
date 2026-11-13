import asyncio
import logging
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest

from hisense_local import gateway as gatewaylib
from hisense_local import wire
from tests import fakes
from tests.fakes import FakeWasher


def make_cert(folder):
    cert, key = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                    "-out", cert, "-days", "1", "-subj", "/CN=test"],
                   check=True, capture_output=True)
    return cert, key


class GatewayCase(unittest.IsolatedAsyncioTestCase):
    gateway_args = {}

    async def asyncSetUp(self):
        self.fake = fakes.FakeSession()
        self.statuses, self.links = [], []
        args = dict(login_timeout=1.0, ack_timeout=0.5, idle_timeout=5.0,
                    on_status=self.statuses.append, on_link=self.links.append)
        args.update(self.gateway_args)
        self.gw = gatewaylib.Gateway(self.fake.session(), **args)
        self.server = await self.gw.serve(0, None, host="127.0.0.1")
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients = []

    async def asyncTearDown(self):
        for c in self.clients:
            c.close()
        self.gw.drop_link()
        self.server.close()
        await self.server.wait_closed()

    async def washer(self, ssl_ctx=None):
        c = await FakeWasher.connect(self.port, ssl=ssl_ctx)
        self.clients.append(c)
        return c

    async def ready_washer(self):
        c = await self.washer()
        await c.login(self.fake)
        for _ in range(50):
            if self.gw.ready:
                break
            await asyncio.sleep(0.01)
        return c


class LoginTest(GatewayCase):
    async def test_login_replies_byte_exact(self):
        c = await self.washer()
        replies = await c.login(self.fake)
        self.assertEqual(replies, [self.fake.login["101"], self.fake.login["102"], self.fake.login["103"]])
        await asyncio.sleep(0.05)
        self.assertTrue(self.gw.ready)
        self.assertEqual(self.links[-1], True)

    async def test_wrong_wid_closed_silently(self):
        c = await self.washer()
        await c.send(self.fake.hello(wid="0" * 36))
        self.assertTrue(await c.closed())

    async def test_frames_before_login_ignored(self):
        c = await self.washer()
        await c.send(self.fake.status([1, 2, 3]))
        await c.send(wire.HEARTBEAT)
        await c.send(b"garbage\r\n")
        await c.send(self.fake.hello())
        self.assertEqual(await c.read(), self.fake.login["101"])
        self.assertEqual(self.statuses, [])

    async def test_heartbeat_and_silent_303(self):
        c = await self.ready_washer()
        await c.send(fakes.line({"id": 303, "token": self.fake.token, "err": 0}))
        await c.send(wire.HEARTBEAT)
        self.assertEqual(await c.read(), wire.HEARTBEAT)

    async def test_status_published(self):
        c = await self.ready_washer()
        await c.send(self.fake.status([9, 0, 239]))
        await c.send(wire.HEARTBEAT)
        await c.read()
        self.assertEqual(self.statuses, [[9, 0, 239]])

    async def test_second_login_replaces_first(self):
        first = await self.ready_washer()
        second = await self.ready_washer()
        self.assertTrue(await first.closed())
        await second.send(wire.HEARTBEAT)
        self.assertEqual(await second.read(), wire.HEARTBEAT)
        self.assertTrue(self.gw.ready)

    async def test_no_login_times_out(self):
        self.gw.login_timeout = 0.2
        c = await self.washer()
        self.assertTrue(await c.closed())

    async def test_oversize_buffer_closed(self):
        c = await self.washer()
        await c.send(b"HS{" + b"x" * (wire.MAX_LINE + 100))
        self.assertTrue(await c.closed())

    async def test_idle_link_dropped(self):
        self.gw.idle_timeout = 0.2
        c = await self.ready_washer()
        self.assertTrue(await c.closed())
        self.assertFalse(self.gw.ready)
        self.assertEqual(self.links[-1], False)

    async def test_half_open_cap(self):
        self.gw.max_half_open = 2
        waiting = [await self.washer() for _ in range(2)]
        await asyncio.sleep(0.05)
        extra = await self.washer()
        self.assertTrue(await extra.closed(timeout=0.5))
        self.assertEqual(len(waiting), 2)


class HostilePeerTest(GatewayCase):
    async def test_odd_ack_does_not_break_the_link(self):
        c = await self.ready_washer()
        await c.send(fakes.line({"id": 201, "seq": [1], "err": {}, "status": 5}))
        await c.send(wire.HEARTBEAT)
        self.assertEqual(await c.read(), wire.HEARTBEAT)

    async def test_handler_error_closes_the_socket(self):
        def boom(values):
            raise RuntimeError("callback bug")
        self.gw.on_status = boom
        c = await self.ready_washer()
        with self.assertLogs("gateway", "WARNING") as logs:
            await c.send(self.fake.status([1, 2, 3]))
            self.assertTrue(await c.closed())
        self.assertNotIn("callback bug", "".join(logs.output))
        self.assertFalse(self.gw.ready)
        self.assertEqual(self.gw.half_open, 0)
        again = await self.ready_washer()  # the gateway still serves
        await again.send(wire.HEARTBEAT)
        self.assertEqual(await again.read(), wire.HEARTBEAT)


class PeerFilterTest(GatewayCase):
    gateway_args = {"peer_ip": "192.0.2.1"}

    async def test_other_peer_closed(self):
        c = await self.washer()
        await c.send(self.fake.hello())
        self.assertTrue(await c.closed())


class PeerAllowedTest(GatewayCase):
    gateway_args = {"peer_ip": "127.0.0.1"}

    async def test_configured_peer_logs_in(self):
        c = await self.washer()
        replies = await c.login(self.fake)
        self.assertEqual(replies[0], self.fake.login["101"])


class NoSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_closed_without_session(self):
        gw = gatewaylib.Gateway(None)
        server = await gw.serve(0, None, host="127.0.0.1")
        c = await FakeWasher.connect(server.sockets[0].getsockname()[1])
        await c.send(fakes.FakeSession().hello())
        self.assertTrue(await c.closed())
        c.close()
        server.close()
        await server.wait_closed()
        self.assertEqual(await gw.send("Actions=2"), "not captured")


class CommandTest(GatewayCase):
    async def answer(self, c, err=0, values=None):
        raw = await c.read()
        seq = wire.parse(raw).body["seq"]
        await c.send(self.fake.ack(seq, err=err, values=values))
        return raw

    async def test_ok_and_status_from_ack(self):
        c = await self.ready_washer()
        task = asyncio.ensure_future(self.gw.exchange("DownLight=1"))
        raw = await self.answer(c, values=[4, 5, 6])
        self.assertEqual(raw, self.fake.frames["DownLight=1"])
        self.assertEqual(await task, ("ok", [4, 5, 6]))
        self.assertIn([4, 5, 6], self.statuses)

    async def test_refused(self):
        c = await self.ready_washer()
        task = asyncio.ensure_future(self.gw.send("Actions=1"))
        await self.answer(c, err=5)
        self.assertEqual(await task, "refused err=5")

    async def test_no_ack(self):
        await self.ready_washer()
        self.assertEqual(await self.gw.send("Actions=1"), "no ack")

    async def test_not_captured_writes_nothing(self):
        c = await self.ready_washer()
        self.assertEqual(await self.gw.send("Steam=1"), "not captured")
        await c.send(wire.HEARTBEAT)
        self.assertEqual(await c.read(), wire.HEARTBEAT)

    async def test_offline_and_logging_in(self):
        self.assertEqual(await self.gw.send("Actions=2"), "washer offline")
        c = await self.washer()
        await c.send(self.fake.hello())
        await c.read()
        self.assertEqual(await self.gw.send("Actions=2"), "logging in")

    async def test_sends_are_serialised(self):
        c = await self.ready_washer()
        first = asyncio.ensure_future(self.gw.send("DownLight=0"))
        second = asyncio.ensure_future(self.gw.send("DownLight=1"))
        raw1 = await c.read()
        with self.assertRaises(asyncio.TimeoutError):
            await c.read(timeout=0.2)  # nothing else is written before the first ack
        await c.send(self.fake.ack(wire.parse(raw1).body["seq"]))
        await self.answer(c)
        self.assertEqual((await first, await second), ("ok", "ok"))

    async def test_in_flight_send_released_by_new_login(self):
        c = await self.ready_washer()
        self.gw.ack_timeout = 5
        task = asyncio.ensure_future(self.gw.send("Actions=2"))
        await c.read()
        await self.ready_washer()
        self.assertEqual(await asyncio.wait_for(task, 1), "washer offline")


class TlsTest(GatewayCase):
    @unittest.skipUnless(shutil.which("openssl"), "openssl missing")
    async def test_login_over_tls(self):
        self.server.close()
        await self.server.wait_closed()
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = make_cert(tmp)
            self.server = await self.gw.serve(0, gatewaylib.server_context(cert, key), host="127.0.0.1")
        self.port = self.server.sockets[0].getsockname()[1]
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        client_ctx.maximum_version = ssl.TLSVersion.TLSv1_2  # what the washer speaks
        c = await self.washer(client_ctx)
        replies = await c.login(self.fake)
        self.assertEqual(replies[2], self.fake.login["103"])
        task = asyncio.ensure_future(self.gw.send("Actions=2"))
        raw = await c.read()
        await c.send(self.fake.ack(wire.parse(raw).body["seq"]))
        self.assertEqual(await task, "ok")


class LogHygieneTest(GatewayCase):
    async def test_logs_hold_no_secrets(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            c = await self.ready_washer()
            await c.send(self.fake.status([1, 2]))
            task = asyncio.ensure_future(self.gw.send("Actions=2"))
            raw = await c.read()
            await c.send(self.fake.ack(wire.parse(raw).body["seq"], err=3))
            await task
            await c.send(fakes.line({"id": 303, "token": self.fake.token}))
            await c.send(b"HS{broken\r\n")
            await c.send(wire.HEARTBEAT)
            await c.read()
            c.close()
            await asyncio.sleep(0.05)
        text = "\n".join(logs.output)
        secrets = [self.fake.wid, self.fake.token, self.fake.key, self.fake.sid]
        secrets += [wire.parse(f).body["com"] for f in self.fake.frames.values()]
        secrets += [r.hex()[:40] for r in self.fake.login.values()]
        for secret in secrets:
            self.assertNotIn(secret, text)
        self.assertIn("ack seq=", text)


if __name__ == "__main__":
    unittest.main()
