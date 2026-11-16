#!/usr/bin/env python3
"""Passive learning: label captured command frames by the status change they cause.

In relay mode the add-on sits between the washer and the real cloud, so the user's
Hisense app keeps working while every command frame (201) the cloud sends is recorded.
A recorded frame has no name: its payload is AES under the session key and its decoded
structure is not solved. This labels it instead by EFFECT — a 201 is followed by a
901/ack whose UART status changes; the catalog's own decoder (field_value) reads each
property's value from those bytes, so a frame earns the name prop=value with no
decryption and for every property kind (plain byte, index map, flag bit, multi-byte sum).

Only user-settable properties are considered; read-only status (machine_status,
minutes_left, water/energy counters) is ignored, because it moves as a CONSEQUENCE of a
command, not as the command. If, after that filter, exactly one settable property
changed, the frame is named; several leave it unlabelled rather than mislabelled (a wrong
label would replay the wrong frame later). Two exceptions: a program change is named as
the program even though temperature, spin and options follow it, and a command that moves
no setting is named START or STOP only when the state goes ready->running or
running->stopped. Pause and resume change no status byte, so they stay unnamed.

This is the engine behind "install, use your washer normally, local control appears":
no ConnectLife account, no command sweep. It is a pure state machine fed (command,
status) events plus a decode function and the set of read-only names to ignore.

Usage:
  python3 passive_learn.py --selftest
"""
from __future__ import annotations

import sys

# status fields that change as a side effect of commands, never as a command themselves
READ_ONLY = frozenset({"machine_status", "minutes_left", "cycle_energy", "cycle_water",
                       "Door_status", "water_used", "energy_used"})


PROGRAM = "Selected_program_ID"
# (state before, state after) -> the frame label the add-on's buttons replay
ACTIONS = {("ready", "running"): "Actions=2",   # start
           ("running", "stopped"): "Actions=1"}  # stop


class Labeler:
    """Fed status/command events in wire order; yields {seq: "prop=val"} as it resolves.

    specs:  {property_name: spec} from the add-on catalog (any spec kind).
    decode: field_value(values, spec) -> the property's value, or None.
    ignore: property names to skip (read-only status); defaults to READ_ONLY."""

    def __init__(self, specs: dict, decode, ignore=READ_ONLY, state_spec=None):
        self.decode = decode
        # machine_status spec ({"at", "map"} naming ready/running/stopped), or None: START
        # and STOP are named only from a ready->running / running->stopped transition
        self.state_spec = state_spec
        # keep only settable properties; a read-only field is an effect, not a command
        self.specs = {name: spec for name, spec in specs.items() if name not in ignore}
        self.last: list[int] | None = None   # last full status seen
        self.pending: int | None = None      # a command seq awaiting its result
        self.labels: dict[str, str] = {}      # seq -> "prop=val"
        self.unresolved: dict[str, str] = {}  # seq -> why it got no label

    def on_command(self, seq: int) -> None:
        if self.pending is not None:
            self.unresolved[str(self.pending)] = "no status followed (no-op?)"
        self.pending = seq

    def on_status(self, values: list[int]) -> None:
        if self.pending is not None:
            if self.last is not None:
                self._resolve(self.pending, self.last, values)
            else:
                self.unresolved[str(self.pending)] = "arrived before any baseline status"
            self.pending = None
        self.last = list(values)

    def _resolve(self, seq: int, before: list[int], after: list[int]) -> None:
        changed = []
        for name, spec in self.specs.items():
            try:
                v_before, v_after = self.decode(before, spec), self.decode(after, spec)
            except (IndexError, ValueError, KeyError, TypeError):
                continue
            if v_after is not None and v_before != v_after:
                changed.append((name, v_after))
        key = str(seq)
        program = [c for c in changed if c[0] == PROGRAM]
        if program:
            # a program change resets temperature, spin and options to that program's
            # defaults, so the other moves are its consequence, not separate commands
            changed = program
        if not changed:
            action = self._action(before, after)
            if action:
                self.labels[key] = action
            else:
                self.unresolved[key] = "no settable property changed"
        elif len(changed) > 1:
            self.unresolved[key] = "several properties changed: " + ", ".join(n for n, _ in changed)
        else:
            name, value = changed[0]
            self.labels[key] = f"{name}={value}"

    def _action(self, before: list[int], after: list[int]) -> str | None:
        """START or STOP, named only from a state transition seen in the status.

        Pause and resume leave every status byte unchanged on the tested washer, and a
        command on the end screen is acknowledged but ignored, so nothing else is named."""
        if self.state_spec is None:
            return None
        try:
            s_before = self.decode(before, self.state_spec)
            s_after = self.decode(after, self.state_spec)
        except (IndexError, ValueError, KeyError, TypeError):
            return None
        return ACTIONS.get((s_before, s_after))

    def report(self) -> dict:
        return {"labelled": dict(self.labels), "unresolved": dict(self.unresolved),
                "count": len(self.labels)}


# ---- offline self-check --------------------------------------------------------------

def selftest() -> None:
    # a decode that mimics the real field_value for the spec kinds that matter here:
    #   int offset, {"at","map"} index map, [offset,mask,on] flag bit
    def decode(values, spec):
        if isinstance(spec, dict):
            raw = values[spec["at"]] if spec["at"] < len(values) else None
            return None if raw is None else spec["map"].get(str(raw), raw)
        if isinstance(spec, int):
            return values[spec] if spec < len(values) else None
        off, mask, on = spec
        if off >= len(values):
            return None
        return 1 if ((values[off] + 1) & 0xFF) & mask == on else 0

    specs = {
        "temperature": 1,                                   # plain byte
        "Selected_program_ID": {"at": 0, "map": {"27": 44, "33": 52}},  # index map
        "Prewash": [2, 16, 16],                             # flag bit of byte 2
        "machine_status": 3,                                # READ-ONLY: must be ignored
        "minutes_left": 4,                                  # READ-ONLY: must be ignored
    }
    # byte2 flag on = (byte2+1)&16==16 -> byte2 in {15,...}; use 15 (on) / 0 (off)

    # a temperature change, with machine_status ALSO moving as a side effect -> still named
    lab = Labeler(specs, decode)
    lab.on_status([27, 4, 0, 32, 99])
    lab.on_command(1)
    lab.on_status([27, 6, 0, 67, 90])        # temp 4->6 AND status 32->67, minutes 99->90
    assert lab.labels == {"1": "temperature=6"}, lab.labels

    # a PROGRAM change (index map 27->33 => id 44->52), status side effect ignored
    lab.on_command(2)
    lab.on_status([33, 6, 0, 32, 80])
    assert lab.labels["2"] == "Selected_program_ID=52", lab.labels

    # a FLAG option toggles on (byte2 0->15), nothing else settable moves
    lab.on_command(3)
    lab.on_status([33, 6, 15, 32, 70])
    assert lab.labels["3"] == "Prewash=1", lab.labels

    # only read-only fields move -> not named (it was not a settable command)
    lab.on_command(4)
    lab.on_status([33, 6, 15, 64, 60])
    assert "4" not in lab.labels and "4" in lab.unresolved

    # two settable properties at once (neither the program) -> ambiguous, refused
    lab.on_command(5)
    lab.on_status([33, 9, 0, 64, 50])        # temperature AND prewash changed
    assert "5" not in lab.labels and "several properties" in lab.unresolved["5"]

    # a program change that also resets temperature -> named as the program
    lab.on_command(6)
    lab.on_status([27, 4, 0, 64, 50])
    assert lab.labels["6"] == "Selected_program_ID=44", lab.labels

    # read-only specs are dropped at construction
    assert "machine_status" not in lab.specs and "minutes_left" not in lab.specs
    assert set(lab.specs) == {"temperature", "Selected_program_ID", "Prewash"}

    # first command before any baseline is held, not left pending
    lab2 = Labeler(specs, decode)
    lab2.on_command(9)
    lab2.on_status([27, 4, 0, 32, 99])
    assert lab2.labels == {} and lab2.pending is None and "9" in lab2.unresolved

    # START / STOP from the state transition; anything else with no settable change stays unnamed
    state = {"at": 3, "map": {"127": "ready", "64": "running", "74": "stopped"}}
    lab3 = Labeler(specs, decode, state_spec=state)
    lab3.on_status([27, 4, 0, 127, 99])
    lab3.on_command(20); lab3.on_status([27, 4, 0, 64, 99])   # ready -> running
    lab3.on_command(21); lab3.on_status([27, 4, 0, 64, 98])   # pause: nothing moves
    lab3.on_command(22); lab3.on_status([27, 4, 0, 74, 98])   # running -> stopped
    assert lab3.labels == {"20": "Actions=2", "22": "Actions=1"}, lab3.labels
    assert "21" in lab3.unresolved
    print("passive_learn selftest OK")


def main() -> None:
    if "--selftest" in sys.argv:
        selftest()
        return
    print(__doc__)


if __name__ == "__main__":
    main()
