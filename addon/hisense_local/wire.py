"""Byte level: the HS line protocol and the UART status block it carries."""
import base64
import json
import re

TERMINATOR = b"\r\n"
MAX_LINE = 4096  # largest frame seen on the wire is under 500 bytes
_TAIL = re.compile(rb"(?:[0-9a-f]{64})?")
HEARTBEAT = b'HS{"id":902}\r\n'


class BadFrame(ValueError):
    pass


class Frame:
    """One HS line: numeric id, decoded JSON body and the exact bytes received."""

    __slots__ = ("msg_id", "body", "raw")

    def __init__(self, msg_id, body, raw):
        self.msg_id, self.body, self.raw = msg_id, body, raw

    def __repr__(self):  # never show the body: it holds tokens and keys
        return f"<Frame id={self.msg_id} len={len(self.raw)}>"


def parse(raw):
    """Parse one complete line (terminator included)."""
    if not raw.startswith(b"HS") or not raw.endswith(TERMINATOR):
        raise BadFrame("no HS prefix or CRLF")
    end = raw.rfind(b"}")
    if end < 0 or not _TAIL.fullmatch(raw[end + 1:-2]):
        raise BadFrame("bad JSON end or signature")
    try:
        body = json.loads(raw[2:end + 1])
        msg_id = int(body["id"])
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
        raise BadFrame(f"bad body: {type(exc).__name__}") from None
    return Frame(msg_id, body, raw)


def seq_of(raw):
    return parse(raw).body.get("seq")


def uart_block(body):
    """Raw UART bytes of the msgtype 102 entry in a status list, or None."""
    status = body.get("status")
    for item in status if isinstance(status, list) else ():
        if isinstance(item, dict) and item.get("msgtype") == 102:
            try:
                return base64.b64decode(item.get("result", ""), validate=True)
            except (ValueError, TypeError):
                return None
    return None


def uart_values(block):
    """Decode an F4 F5 ... F4 FB block into (values, checksum_ok).

    Values are the bytes between the start mark and the 2-byte checksum,
    each one inverted and lowered by one. A doubled F4 inside counts once.
    The checksum looks like a 16-bit sum of those bytes; it is reported,
    not enforced, because only two real samples back it.
    """
    if len(block) < 6 or block[:2] != b"\xf4\xf5" or block[-2:] != b"\xf4\xfb":
        raise BadFrame("not a UART status block")
    inner = block[2:-2].replace(b"\xf4\xf4", b"\xf4")
    data, check = inner[:-2], int.from_bytes(inner[-2:], "big")
    values = [((b ^ 0xFF) - 1) & 0xFF for b in data]
    return values, (sum(data) & 0xFFFF) == check


def status_values(body):
    """Values from a 901 or 201-ack body, or None when it carries no UART block."""
    block = uart_block(body)
    if block is None:
        return None
    try:
        return uart_values(block)[0]
    except BadFrame:
        return None


def field_value(values, spec):
    """Read one field from decoded status values.

    spec is an offset (the byte holds the value), {"at": offset, "map": {byte: value}}
    for a byte that holds an internal index for some values, or [offset, mask, on]
    for a flag stored as one bit of the inverted byte.
    """
    if isinstance(spec, dict):
        raw = field_value(values, spec["at"])
        return None if raw is None else spec["map"].get(str(raw), raw)
    if isinstance(spec, int):
        return values[spec] if spec < len(values) else None
    offset, mask, on = spec
    if offset >= len(values):
        return None
    return 1 if ((values[offset] + 1) & 0xFF) & mask == on else 0


def valid_spec(spec):
    """True when field_value can read spec without raising (specs come from a JSON file)."""
    if isinstance(spec, dict):
        return (type(spec.get("at")) is int and spec["at"] >= 0 and isinstance(spec.get("map"), dict)
                and all(isinstance(v, (int, str)) for v in spec["map"].values()))
    if isinstance(spec, list):
        return len(spec) == 3 and all(type(x) is int for x in spec) and spec[0] >= 0
    return type(spec) is int and spec >= 0
