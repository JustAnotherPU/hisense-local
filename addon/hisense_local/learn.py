"""Learn mode: build the local session passively from normal app use.

In relay mode every command the user sends through the Hisense app is recorded. This
names each recorded 201 by the status change it causes (passive_learn.Labeler), then at
the end of a cloud session rebuilds it as a labelled, replayable session and promotes it
if it covers more commands than the one in use.

Why per-session and not merged: each frame's trailer is a MAC bound to its session key
(a proven dead end), so frames from different logins cannot be combined. The richest
single session the user happens to produce — e.g. setting up a full wash in one go —
becomes the local library. Coverage grows as the user does richer sessions, never by
merging.

Wiring: Capture(learn=True, on_cmd=..., on_close=...) feeds on_cmd(seq) per recorded 201;
the gateway's status path feeds on_status(values); on_close(record) promotes.
"""
import logging

from . import session as sessionlib
from . import wire
from .passive_learn import Labeler

log = logging.getLogger("learn")


def relabel(record, labels):
    """A session dict from a capture record, frames keyed prop=val via {seq_str: name}.

    Keeps a frame only when it is a 201 whose seq matches its stored unlabelled_<seq>
    key — the same safety the import path applies."""
    by_seq = {label.rsplit("_", 1)[-1]: hexframe
              for label, hexframe in record.get("frames", {}).items()}
    frames = {}
    for seq, name in labels.items():
        hexframe = by_seq.get(seq)
        if hexframe is None:
            continue
        try:
            frame = wire.parse(bytes.fromhex(hexframe))
        except (wire.BadFrame, ValueError):
            continue
        if frame.msg_id == 201 and str(frame.body.get("seq")) == seq:
            frames[name] = hexframe
    return {
        "id": record.get("id"),
        "wid": record.get("wid"),
        "recorded_at": record.get("recorded_at"),
        "state": "ready",
        "login": {k: record["login"][k] for k in ("101", "102", "103")
                  if k in record.get("login", {})},
        "frames": frames,
    }


def should_promote(new_count, active_count):
    """Promote only a strictly richer session, so a thin one never replaces a full one."""
    return new_count > active_count


class Learn:
    def __init__(self, data, offsets):
        self.data = data
        # the labeller names a command by which settable property's decoded value moved;
        # field_value reads every spec kind (byte, index map, flag bit, multi-byte sum)
        self.specs = {k: v for k, v in (offsets or {}).items() if wire.valid_spec(v)}
        # an unsettled machine_status (a candidate list) fails valid_spec: no START/STOP naming
        self.state_spec = self.specs.get("machine_status")
        self.labeler = self._labeler()

    def _labeler(self):
        return Labeler(self.specs, wire.field_value, state_spec=self.state_spec)

    def on_cmd(self, seq):
        self.labeler.on_command(seq)

    def on_status(self, values):
        self.labeler.on_status(values)

    def on_close(self, record):
        labels = self.labeler.report()["labelled"]
        self.labeler = self._labeler()  # fresh baseline for the next login
        if not self.specs:
            log.warning("no calibration offsets: cannot name commands yet (run calibrate)")
            return
        session = relabel(record, labels)
        if len(session["login"]) < 3 or not session["frames"]:
            log.info("learn: session %s has %d named frames, incomplete: not promoted",
                     (session["id"] or "?")[:4], len(session["frames"]))
            return
        try:
            loaded = sessionlib.Session(session)
            active = self.data.load_active("")
            active_count = len(active.labels) if active else 0
            if should_promote(len(loaded.labels), active_count):
                self.data.save(loaded)
                self.data.activate(loaded.id)
                log.warning("learn: promoted session %s with %d commands (was %d); restart to serve",
                            loaded.id[:4], len(loaded.labels), active_count)
            else:
                log.info("learn: session %s has %d commands, not more than the active %d: kept",
                         loaded.id[:4], len(loaded.labels), active_count)
        except sessionlib.SessionError as exc:
            log.info("learn: session not usable yet (%s)", exc)


# ---- offline self-check --------------------------------------------------------------

def _selftest():
    import json

    def frame(seq):
        js = json.dumps({"id": 201, "seq": seq, "com": "AAAA", "cmdversion": -1}).encode()
        return (b"HS" + js + b"ab01" * 16 + b"\r\n").hex()

    rec = {"id": "s", "wid": "w", "recorded_at": 1.0,
           "login": {"101": "aa", "102": "bb", "103": "cc"},
           "frames": {"unlabelled_10": frame(10), "unlabelled_11": frame(11),
                      "unlabelled_12": frame(999)}}  # 12's frame seq mismatches -> dropped
    labels = {"10": "temperature=6", "11": "Selected_program_ID=11", "12": "x=1"}
    out = relabel(rec, labels)
    assert set(out["frames"]) == {"temperature=6", "Selected_program_ID=11"}, out["frames"]
    assert out["state"] == "ready" and len(out["login"]) == 3

    # incomplete login is caught by the caller via len(login) < 3
    thin = relabel({"id": "s", "wid": "w", "login": {"101": "a"}, "frames": {}}, {})
    assert len(thin["login"]) == 1 and thin["frames"] == {}

    assert should_promote(5, 3) and not should_promote(3, 3) and not should_promote(2, 4)
    print("learn selftest OK")


if __name__ == "__main__":
    _selftest()
