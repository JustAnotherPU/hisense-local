# hisense-local

hisense-local runs a Hisense washing machine without the vendor cloud. The washer's Wi-Fi module
logs in to a Home Assistant add-on on your network instead of the internet. The add-on replays a
login recorded once from the real cloud, reads the machine's status and exposes it over MQTT:
program, temperature, spin, delay, options, child lock, drum light, and start, pause, resume and
stop buttons.

It needs no cloud account at run time and does not touch the washer's firmware.

Supported models: WF5I1045BWQ only, so far. Does my model work? If you try another one, report
the result with the [model report form](.github/ISSUE_TEMPLATE/model-report.yml).

- [Quick start for the WF5I1045BWQ](#quick-start-for-the-wf5i1045bwq)
- [Capture guide](docs/capture-guide.md): redirecting the washer and recording a session.
- [Limits](docs/limits.md): what was verified, what was not, and what can break it.

## Quick start for the WF5I1045BWQ

This model has been tested, and the add-on ships its status readings, so you can skip the
calibration and the learning wash. You need Home Assistant with an MQTT broker, a router that can
redirect one connection, and your vendor account for a single capture.

1. Install the add-on (see [Install](#install)). Set `model` to `WF5I1045BWQ` and fill in the MQTT
   options.
2. On the router, send the washer's TLS connection to the add-on and set `peer_ip`
   ([capture guide, step 1](docs/capture-guide.md#1-redirect-the-washers-tls-connection-to-the-add-on)).
3. Record a session once with the driver script and `--with-cycle`, drum empty
   ([capture guide, steps 2 to 4](docs/capture-guide.md#2-arm-the-capture)).
4. Block the washer from the internet and keep the redirect.
5. Restart the add-on. Program, time left and state show up as soon as the washer connects.

Step 3 has seen the least real use. The capture code has only run against a stand-in cloud, and
the test washer's session was recorded with an earlier tool (see [limits](docs/limits.md#other)).
If a step fails, open an issue with the add-on log. Logs never contain keys or the washer id.

## How it works

The Wi-Fi module keeps one TLS connection open to the vendor's server and talks in short JSON
lines. A router rule sends that connection to the add-on. The module accepts a self-signed
certificate and does not check that the login replies are fresh, so the add-on answers with replies
recorded during a capture. Status messages carry the machine's serial frame, which the add-on
decodes.

Commands are signed with a secret this project does not know, so the add-on cannot build new
ones. For each setting it replays, byte for byte, a frame the real cloud sent during the capture.
The same value always gives the same frame, so one recording per value is enough.

## Requirements

- Home Assistant OS or Supervised, with an MQTT broker (the Mosquitto add-on works).
- A router that can redirect the washer's outgoing TLS connection to the add-on and block the
  washer from the internet. See [step 1 of the capture guide](docs/capture-guide.md#1-redirect-the-washers-tls-connection-to-the-add-on).
- A session recorded from the vendor cloud: see [Sessions](#sessions).

## Install

1. In Home Assistant, open Settings > Add-ons > Add-on store, open the menu, choose Repositories
   and add `https://github.com/JustAnotherPU/hisense-local`.
2. Install "Hisense Local".
3. Set the options, put a session in place (next section), then start it.

To install by hand instead, run `tools/pack_addon.sh`, which writes `dist/hisense_local.tar.gz`,
and unpack it into the `/addons` folder of Home Assistant (`/addons/hisense_local/`). Then use
Check for updates in the add-on store menu and install "Hisense Local". Do not keep two copies with
the same slug.

| Option | Default | Meaning |
|---|---|---|
| `mqtt_url` | `mqtt://core-mosquitto:1883` | Broker URL; `mqtts://` for TLS |
| `mqtt_user`, `mqtt_password` | empty | Broker login |
| `peer_ip` | empty | Accept the washer link only from this address (the router's, when it masquerades) |
| `model` | `other` | `WF5I1045BWQ` uses the built-in status readings when no calibration has run |
| `discovery` | `device` | `device` (Home Assistant 2024.11+) or `per_entity` |
| `active_session` | empty | Session to replay, by id or its first characters; empty uses the active one |
| `capture_date` | empty | `YYYY-MM-DD`: arms a capture for that day only |
| `cloud_addr` | empty | IP address of the vendor cloud, used only by a capture |
| `calibrate` | `false` | Run a calibration instead of serving, then stop |
| `calibrate_cycle` | `false` | Include a short start/pause/resume/stop in the calibration |

The add-on publishes only port 4444/tcp. It has no web page.

## Sessions

A session is the washer id, three recorded login replies and the labelled command frames. Session
files live in the add-on's `/data/sessions/` with mode 0600. Whoever holds one can drive your washer
from your network, so keep any copy private and outside this repository.

### Import

If you already hold capture files (`login.json`, `commands.json`, `library.json`), build a session
file on your computer, from the root of this repository:

```sh
PYTHONPATH=addon python3 -m hisense_local import-session --login login.json \
    --commands commands.json --library library.json --out session.json
```

Copy `session.json` to `/share/hisense_local/import/`. At start the add-on moves it into `/data`,
makes it active if none is, and deletes the shared copy, since other add-ons can read `/share`.

### Capture

A capture lets one washer login through to the real cloud and records it, with one frame per
setting. The steps are in the [capture guide](docs/capture-guide.md). A first capture, with no
session yet, needs `peer_ip`.

## Calibration

Skip this section on a WF5I1045BWQ with `model` set: its readings are built in. A calibration
still runs on that model if you turn it on, and its result then replaces the built-in one.

Turn `calibrate` on and start the add-on with the washer on, panel on, drum empty, door closed. It
steps through programs, temperatures, spin speeds, delay values and options, compares the status
returned with each acknowledgement, and writes `/data/status_offsets.json` and a full report in
`/share/hisense_local/calibration.json`. At the end it puts program, child lock and drum light back
to the values it read at the start. If it could not find a value's position, it sets Eco 40-60 and
child lock off instead. Turn `calibrate` off and restart.

With `calibrate_cycle` it also runs start, pause, resume and stop on the quick program and records
every status, which narrows down the state reading. The short test cycle is too brief for the
minute counter to move, so the time-left and state readings are settled during the first real wash
afterwards: the add-on watches which copy counts down, saves the result and adds the State sensor.
See [limits](docs/limits.md#status-readings).

To decode every status frame found as hex in a capture log, offline:

```sh
PYTHONPATH=addon python3 -m hisense_local selfcheck --decode FILE --expect 239
```

## Entities

All entities belong to one device. Controls are offered only for values that have a recorded frame,
and are available only while both the add-on and the washer link are up.

- Selects: program (18 dial positions), temperature, spin, delay start (0 to 24 h).
- Switches: any option recorded both on and off, plus child lock and drum light. Which options
  the test machine accepts is in [limits](docs/limits.md).
- Buttons: start, pause, resume, stop (recorded with `--with-cycle`).
- Sensors: time left in minutes and State, which reads `ready`, `running`, `stopped`, `done` (end
  screen) or `off` (the washer stopped answering). They come from the built-in readings or a
  calibration. Diagnostics: last command result, recorded frames, session, mode.

Commands go to `hisense_local/set/<property>`. Each result is published on `hisense_local/result`:
`ok`, `refused err=N`, `no ack`, `not captured`, `not offered`, `washer offline`, `logging in` or
`capture running`. Retained messages on command topics are ignored, so a stale message on the
broker can never start the machine.

## Security notes

- Nothing is answered before a login with the recorded washer id. Wrong id, no login within 15 s,
  a line over 4 KB, or more than four connections waiting for login: the connection is closed.
- Local mode never opens an outbound connection. The capture code is loaded only when a capture is
  configured.
- Logs hold frame ids, lengths, sequence numbers, error codes and a short session id. They never
  hold tokens, keys, the washer id or command blobs.
- `tools/check_clean.sh` fails on secret-shaped strings in the tree and its history.

## Tests

From the root of the repository:

```sh
python3 -m unittest
```

Standard library only: no network, no broker. A fake washer client logs in over TLS on localhost
(skipped if `openssl` is missing), and a fake cloud covers the capture relay. CI runs the tests and
`tools/check_clean.sh`.

## Licence

MIT, see [LICENSE](LICENSE).

This project is not affiliated with or endorsed by Hisense; names are trademarks of their owners.
