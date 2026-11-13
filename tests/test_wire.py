import asyncio
import os
import random
import unittest

from hisense_local import wire
from tests import fakes


class ParseTest(unittest.TestCase):
    def test_signed_and_unsigned(self):
        signed = fakes.line({"id": 201, "seq": 5, "com": "abc"})
        frame = wire.parse(signed)
        self.assertEqual((frame.msg_id, frame.body["seq"]), (201, 5))
        self.assertEqual(frame.raw, signed)
        self.assertEqual(wire.parse(wire.HEARTBEAT).msg_id, 902)

    def test_garbage(self):
        for raw in (b"", b"HS\r\n", b"XX{\"id\":1}\r\n", b"HS{\"id\":1}", b"HS{\"id\":1}zz\r\n",
                    b"HS{\"id\":1}" + b"a" * 63 + b"\r\n", b"HS[1,2]\r\n", b"HS{\"no\":1}\r\n",
                    b"HS{\"id\":\"x\"}\r\n", os.urandom(40) + b"\r\n"):
            with self.assertRaises(wire.BadFrame, msg=raw):
                wire.parse(raw)

    def test_repr_hides_body(self):
        frame = wire.parse(fakes.line({"id": 102, "key": "SECRETVALUE"}))
        self.assertNotIn("SECRET", repr(frame))


class StreamSplitTest(unittest.TestCase):
    """The gateway splits with StreamReader.readuntil; check chunk cuts and the size cap."""

    def split(self, data, cuts):
        async def run():
            reader = asyncio.StreamReader(limit=wire.MAX_LINE)
            pos = 0
            for cut in cuts + [len(data)]:
                reader.feed_data(data[pos:cut])
                pos = cut
            reader.feed_eof()
            out = []
            while True:
                try:
                    out.append(await reader.readuntil(wire.TERMINATOR))
                except asyncio.IncompleteReadError:
                    return out
        return asyncio.run(run())

    def test_chunks_cut_anywhere(self):
        frames = [fakes.line({"id": 901, "n": n}) for n in range(5)] + [wire.HEARTBEAT]
        data = b"".join(frames)
        rnd = random.Random(7)
        for _ in range(50):
            cuts = sorted(rnd.sample(range(1, len(data)), 12))
            self.assertEqual(self.split(data, cuts), frames)
        # cut between CR and LF
        first = len(frames[0]) - 1
        self.assertEqual(self.split(data, [first]), frames)

    def test_oversize(self):
        async def run():
            reader = asyncio.StreamReader(limit=wire.MAX_LINE)
            reader.feed_data(b"HS{" + b"x" * (wire.MAX_LINE + 10))
            with self.assertRaises((asyncio.LimitOverrunError, ValueError)):
                await reader.readuntil(wire.TERMINATOR)
        asyncio.run(run())


class UartTest(unittest.TestCase):
    def test_round_trip(self):
        values = [9, 0, 239, 14, 3, 200]
        out, ok = wire.uart_values(fakes.uart(values))
        self.assertEqual(out, values)
        self.assertTrue(ok)

    def test_doubled_f4(self):
        # value 10 encodes to 0xF4, so it must appear doubled on the wire
        block = fakes.uart([1, 10, 2])
        self.assertIn(b"\xf4\xf4", block[2:-2])
        self.assertEqual(wire.uart_values(block)[0], [1, 10, 2])

    def test_checksum_reported_not_enforced(self):
        values, ok = wire.uart_values(fakes.uart([5, 6, 7], checksum=1))
        self.assertEqual(values, [5, 6, 7])
        self.assertFalse(ok)

    def test_bad_block(self):
        for block in (b"", b"\xf4\xf5\xf4\xfb", b"\x00\x01\x02\x03\x04\x05\x06"):
            with self.assertRaises(wire.BadFrame):
                wire.uart_values(block)

    def test_status_values_from_body(self):
        body = fakes.status_body(901, [9, 239], "t")
        self.assertEqual(wire.status_values(body), [9, 239])
        self.assertIsNone(wire.status_values({"id": 901}))
        self.assertIsNone(wire.status_values({"status": [{"msgtype": 102, "result": "!!"}]}))

    def test_hostile_json_is_a_bad_frame(self):
        with self.assertRaises(wire.BadFrame):
            wire.parse(b'HS{"id":Infinity}\r\n')
        try:  # deep nesting: parsed or refused depending on the interpreter, never another error
            wire.parse(b'HS{"id":1,"a":' + b"[" * 1900 + b"]" * 1900 + b"}\r\n")
        except wire.BadFrame:
            pass

    def test_status_of_the_wrong_type_is_no_status(self):
        for body in ({"status": 5}, {"status": {"msgtype": 102}}, {"status": [{"msgtype": 102, "result": 7}]}):
            self.assertIsNone(wire.status_values(body))


if __name__ == "__main__":
    unittest.main()
