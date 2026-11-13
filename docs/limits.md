# Limits

Everything here comes from one machine.

## Tested hardware

One model: Hisense WF5I1045BWQ. Other models may use the same Wi-Fi module and protocol, but none
has been tried. If you try one, please file a model report (see the README).

## Verified on the machine

These commands were sent and the washer visibly did what was asked:

- All 18 programs on the dial.
- Temperatures: cold, 20, 30, 40, 60 and 90 °C.
- Spin speeds from 0 (no spin) to 1400 rpm.
- Prewash, quicker mode, intensive wash.
- Start, pause, resume and stop.

## Accepted, effect not confirmed

The washer acknowledges these, but no change shows up in the decoded status, so their effect is
unconfirmed:

- Extra rinse, time save, eco.
- Delay start.
- Dose, water level, soil level.
- Child lock, sound.

Dose amounts, water level and soil level are recorded during a capture but get no entity in Home
Assistant.

## Not supported on this model

The washer refuses these options: AquaPreserve, PreSoak, extra_soft, WarmWaterWashing and
WindDrying. They are not recorded and get no entity.

Other option names in the add-on's catalog (steam, anti-crease and so on) appear in Home Assistant
only when a capture recorded them with both on and off. The lists above are all that was checked.

## Status readings need a calibration

The status frame carries the machine's settings and state, but where each value sits differs from
model to model. No position is assumed; a calibration finds them and writes
`/data/status_offsets.json`. Until one has run, controls show the last acknowledged command and the
status sensors (state, phase, door, time left) do not appear.

The calibration copes with two quirks seen on the test machine:

- Program ids above 20 are stored as a dial position, not as the id. The calibration learns that
  mapping.
- The washer keeps or clamps some values: temperature 1 is accepted but changes nothing, and 90 °C
  on a program that stops at 60 °C reads back as a lower level. Those steps do not break the match.

The time-left byte starts out provisional. Before any cycle has run, the program duration appears
in several places and the calibration picks the first one, which may be the total time rather than
the countdown. During the next wash the add-on keeps the copy that drops by one minute while the
others hold, twice in a row, and saves it. The State sensor is settled the same way, from the bytes
the test cycle left as candidates. If two candidates still read "running" at the end of a wash,
the State sensor stays off rather than guess.

## Firmware

Once the cloud is cut off, the washer gets no firmware updates. An update could change the login or
the command format and end local control, so keep the washer off the internet.

## Other

- Only recorded values can be sent. A value that was never captured does not appear in Home
  Assistant.
- The washer's clock comes from the recorded login, so it shows the capture date. Relative delay
  start is not affected; anything based on the time of day may be.
- Capture mode has only run against a stand-in cloud in the tests. The session on the tested
  washer was recorded with an earlier tool and imported. A first real capture may hit something the
  tests did not cover. When the add-on sees a cloud frame it does not expect, it drops the link.
  See the [capture guide](capture-guide.md).
- The washer switches itself off after some idle minutes, and only its power button brings it
  back. Home Assistant cannot start a wash while it is off.
- Once a cycle has finished, the washer acknowledges new program and start commands but ignores
  them. Open the door or switch it off and on before the next remote start.
