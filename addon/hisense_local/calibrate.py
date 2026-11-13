"""Find the status byte for each setting by sending it and watching the ack.

Run with the washer on, panel on, drum empty, door closed. Settings only;
the START/PAUSE/RESUME/STOP cycle is a separate opt-in.
"""
import asyncio
import logging
import time

from . import catalog, wire
from .bridge import controls

log = logging.getLogger("calibrate")


def plan(labels):
    have = controls(labels)
    steps = [("Selected_program_ID", v) for v in sorted(have.get("Selected_program_ID", ()))]
    steps.append(("Selected_program_ID", 8))  # widest temperature and spin range
    steps += [("temperature", v) for v in sorted(have.get("temperature", ()))]
    steps += [("Spin_speed_rpm", v) for v in sorted(have.get("Spin_speed_rpm", ()))]
    steps += [("DelayStartTime", v) for v in (1, 2, 3, 0)]
    steps.append(("Selected_program_ID", 42))  # accepts most options
    for prop in catalog.CALIBRATION_PROPS:
        if prop not in catalog.SELECTS:
            steps += [(prop, 1), (prop, 0)]
    return [f"{p}={v}" for p, v in steps if v in have.get(p, ())]


def diff(before, after):
    return [(i, a, b) for i, (a, b) in enumerate(zip(before, after)) if a != b]


def flag_bits(samples):
    """For a 0/1 property, bytes where on and off differ by exactly one bit: [[offset, mask, on]]."""
    raw = {0: [], 1: []}
    for val, values in samples:
        if val in raw:
            raw[val].append([(v + 1) & 0xFF for v in values])  # undo the decrement first
    if not raw[0] or not raw[1]:
        return []
    size = min(len(v) for v in raw[0] + raw[1])
    hits = []
    for i in range(size):
        on, off = {v[i] for v in raw[1]}, {v[i] for v in raw[0]}
        if len(on) == 1 and len(off) == 1:
            mask = on.pop() ^ off.pop()
            if mask and mask & (mask - 1) == 0:
                hits.append([i, mask, raw[1][0][i] & mask])
    return hits


def indexed(samples):
    """Bytes that hold the value for most settings and a fixed internal index for the rest.

    The dial stores program ids above 20 as a position (42 -> 26...). Returns
    [{"at": offset, "map": {byte: value}}] for bytes where the byte to value mapping is
    one to one and the byte equals the value for at least half the samples.
    """
    size = min(len(v) for _, v in samples)
    hits = []
    for i in range(size):
        pairs = {(v[i], val) for val, v in samples}
        to_val, to_byte = {}, {}
        for b, val in pairs:
            to_val.setdefault(b, set()).add(val)
            to_byte.setdefault(val, set()).add(b)
        if any(len(x) > 1 for x in list(to_val.values()) + list(to_byte.values())):
            continue
        same = sum(1 for b, val in pairs if b == val)
        if same * 2 >= len(pairs):
            hits.append({"at": i, "map": {str(b): val for b, val in sorted(pairs) if b != val}})
    return hits


def derive(steps, timeline=()):
    """steps: [{"label", "result", "values"}] -> ({field: spec}, {field: [candidates]}).

    A spec is a byte offset, or [offset, mask, on] for a one-bit flag.
    """
    by_prop = {}
    for step in steps:
        if step["result"] == "ok" and step.get("values"):
            prop, _, value = step["label"].partition("=")
            by_prop.setdefault(prop, []).append((int(value), step["values"]))

    def matching(samples, expected):
        size = min(len(v) for _, v in samples)
        return [i for i in range(size) if all(v[i] == expected(val) for val, v in samples)]

    found, ambiguous = {}, {}

    def settle(field, hits):
        if len(hits) == 1:
            found[field] = hits[0]
        elif hits:
            ambiguous[field] = hits

    def holding(samples):
        """Bytes equal to the value sent, except where the washer kept or clamped it to
        another level of the same setting (temperature 1, 90 degrees on a 60-degree program)."""
        sent = {val for val, _ in samples}
        size = min(len(v) for _, v in samples)
        return [i for i in range(size)
                if all(v[i] == val or v[i] in sent for val, v in samples)
                and len({val for val, v in samples if v[i] == val}) >= 2]

    for prop, samples in by_prop.items():
        if len({val for val, _ in samples}) < 2:
            continue  # one value tells nothing
        hits = matching(samples, lambda val: val) or holding(samples)
        if not hits and {val for val, _ in samples} == {0, 1}:
            hits = flag_bits(samples)
        if not hits and prop == "Selected_program_ID":
            hits = indexed(samples)  # only the dial stores an internal index
        settle(prop, hits)

    timed = [(v, vals) for v, vals in by_prop.get("Selected_program_ID", ()) if v in catalog.KNOWN_DURATIONS]
    if len({v for v, _ in timed}) >= 2:
        hits = matching(timed, lambda val: catalog.KNOWN_DURATIONS[val])
        running = [t["values"] for t in timeline if t.get("values")]
        if len(hits) > 1 and running:
            # the duration is stored several times; time left is the copy that moves in a cycle
            hits = [i for i in hits if len({v[i] for v in running if i < len(v)}) > 1] or hits
        settle("minutes_left", hits)
        if len(hits) > 1:
            # no copy moved (no cycle, or one too short to tick): every copy holds the duration.
            # The Learner settles it during the next real cycle.
            found["minutes_left"] = hits[0]
    settle("machine_status", state_candidates(timeline))
    return found, ambiguous


def state_candidates(timeline):
    """Bytes with one value at rest, another all through the cycle and a third after the stop.

    Returns [{"at": offset, "map": {byte: "ready" | "running" | "stopped"}}].
    """
    marks = [k for k, e in enumerate(timeline) if e.get("result") == "ok"]
    start = next((k for k in marks if timeline[k]["label"] == "Actions=2"), None)
    stop = next((k for k in marks if timeline[k]["label"] == "Actions=1"), None)
    if start is None or stop is None or stop < start:
        return []
    rows = lambda a, b: [e["values"] for e in timeline[a:b] if e.get("values")]
    before, during, after = rows(0, start), rows(start, stop), rows(stop, len(timeline))
    if not (before and during and after):
        return []
    rest, end = before[-1], after[-1]
    size = min(len(v) for v in [rest, end] + during)
    hits = []
    for i in range(size):
        run = {v[i] for v in during}
        if len(run) == 1 and rest[i] not in run and end[i] not in run:
            names = {str(end[i]): "stopped", str(rest[i]): "ready", str(run.pop()): "running"}
            hits.append({"at": i, "map": names})
    return hits


class Learner:
    """Settles what calibration left open, from status frames seen during a real cycle.

    The time-left byte drops by one minute while the machine runs, twice (identical copies
    drop together). The state byte is the candidate that reads "running" at every tick.
    """

    TICKS = 2

    def __init__(self, candidates, clock=None):
        self.minutes = [i for i in candidates.get("minutes_left", ()) if type(i) is int and i >= 0]
        self.states = [s for s in candidates.get("machine_status", ())
                       if isinstance(s, dict) and wire.valid_spec(s) and "running" in s["map"].values()]
        self.clock = None if self.minutes else clock  # a time-left byte already settled
        self.ticks, self.prev = {}, None

    def remaining(self):
        out = {}
        if self.minutes:
            out["minutes_left"] = list(self.minutes)
        if self.states:
            out["machine_status"] = list(self.states)
        return out

    def feed(self, values):
        """Returns {field: spec} for anything settled by this status frame."""
        prev, self.prev = self.prev, values
        pool = [self.clock] if self.clock is not None else self.minutes
        if not prev or not pool or not self.states and self.clock is not None:
            return {}
        if len(prev) != len(values) or max(pool) >= len(values):
            return {}
        down = [i for i in pool if values[i] == prev[i] - 1]
        # copies of the time left drop together and agree; any other pool byte must hold
        if not down or len({values[i] for i in down}) != 1 or any(
                values[i] != prev[i] for i in pool if i not in down):
            return {}
        running = [s for s in self.states
                   if s["at"] < len(values) and s["map"].get(str(values[s["at"]])) == "running"]
        if self.states and not running:
            return {}  # nothing says the machine runs: a dial turn, not a countdown
        self.states = running
        out = {}
        if self.clock is None:
            self.ticks[down[0]] = self.ticks.get(down[0], 0) + 1
            if self.ticks[down[0]] < self.TICKS:
                return {}
            self.clock, self.minutes = down[0], []  # identical copies: the first is as good as any
            out["minutes_left"] = down[0]
        if len(self.states) == 1 or self.states and values[self.clock] <= 1:
            # shortcut: several bytes held "running" all cycle; take the one naming most states
            # (ready/running/stopped), lowest offset on a tie. Upgrade if a model reports a wrong pick.
            pick = min(self.states, key=lambda s: (-len(set(s["map"].values())), s["at"]))
            self.states = []
            out["machine_status"] = pick
        return out


async def run(gateway, latest, *, with_cycle=False, pause=1.0, cycle_wait=(20, 10, 10, 10)):
    """latest() returns the last decoded status (list) or None."""
    snapshot = latest()
    steps, timeline = [], []
    started = False
    report = {"started": time.time(), "steps": steps, "timeline": timeline}
    labels = plan(gateway.session.labels)
    try:
        for label in labels:
            result, values = await gateway.exchange(label)
            prev = next((s["values"] for s in reversed(steps) if s.get("values")), snapshot)
            steps.append({"label": label, "result": result, "values": values,
                          "diff": diff(prev, values) if prev and values else None})
            log.info("%s -> %s", label, result)
            if result in ("washer offline", "logging in"):
                raise RuntimeError(f"washer link lost at {label}")
            await asyncio.sleep(pause)
        cycle = (f"Selected_program_ID={catalog.QUICK_PROGRAM}", "Actions=2", "Remote_hold=1",
                 "Remote_hold=0", "Actions=1")
        if with_cycle and not set(cycle) <= set(gateway.session.labels):
            log.warning("cycle skipped: it needs the quick program, start, pause, resume and stop frames")
            with_cycle = False
        if with_cycle:
            for label, wait in zip(cycle, (2,) + tuple(cycle_wait)):
                started = started or label == "Actions=2"  # an unacknowledged START may still have started
                result, _ = await gateway.exchange(label)
                if label == "Actions=1" and result == "ok":
                    started = False
                timeline.append({"t": time.time(), "label": label, "result": result})
                if result != "ok" and label in cycle[:2]:
                    log.warning("cycle abandoned: %s -> %s", label, result)
                    break  # never start on another program, never time a cycle that did not start
                end = time.time() + wait
                while time.time() < end:
                    timeline.append({"t": time.time(), "values": latest()})
                    await asyncio.sleep(min(1.0, wait))
    finally:
        restore = []
        if started:  # aborted mid-cycle: stop the drum before anything else
            restore.append(("Actions=1", (await gateway.exchange("Actions=1"))[0]))
        try:
            found, ambiguous = derive(steps, timeline)
        except Exception as exc:  # the washer is put back whatever the analysis does
            log.error("no offsets derived (%s)", type(exc).__name__)
            found, ambiguous = {}, {}
        report.update(offsets=found, ambiguous=ambiguous)
        for prop, fallback in (("Selected_program_ID", catalog.DEFAULT_PROGRAM), ("Child_lock", 0),
                               ("DownLight", None)):
            start = wire.field_value(snapshot, found[prop]) if prop in found and snapshot else None
            start = fallback if start is None else start
            if start is not None:
                restore.append((f"{prop}={start}", (await gateway.exchange(f"{prop}={start}"))[0]))
        report["restored"] = restore
        after = latest()
        report["still_different"] = diff(snapshot, after) if snapshot and after else None
    return report
