# Changelog

## 0.2.1 (unreleased)

- Learn mode names program changes, even though temperature, spin and options move with them.
- Learn mode names Start and Stop when the State goes from ready to running or from running to
  stopped. Pause and resume change nothing in the status, so they stay unnamed.

## 0.2.0 (unreleased)

- Learn mode. With `learn_mode` on, the add-on relays each login to the cloud and records the
  commands you send from the phone app, naming each one by the setting it changes: the program,
  temperature, spin or an option. Local control builds up as you use the machine, with no capture
  run needed. The session with the most commands becomes the active one; a smaller one does not
  replace it.
- A panel inside Home Assistant, through Ingress. It shows whether the washer is connected, how
  many commands are known, and a button to switch between learning and local control.

## 0.1.8 (unreleased)

- Prebuilt images for amd64 and aarch64 on GitHub's container registry, so installing no longer
  builds on the Home Assistant machine.
- README: a button that adds the repository to Home Assistant in one click.

## 0.1.7 (unreleased)

- New `model` option. With `WF5I1045BWQ` and no calibration, the add-on uses the readings settled
  on the tested machine, so the status sensors appear right after the first capture.
- `tools/unifi-redirect.sh` sets up the redirect on a UniFi gateway and keeps it across reboots.
  Every iptables call waits for the lock, since UniFi rewrites its own rules at the same time.
  `ensure` reinstalls the redirect only when a firmware update has removed it, for use from a
  scheduler on another machine.
- README: a quick start for the WF5I1045BWQ.
- A State value that no cycle has named reads as unknown instead of a raw number.

## 0.1.6 (unreleased)

- The State sensor reads "done" on the end screen, where the washer keeps reporting its running
  value with no time left, and "off" once a command goes unanswered. The next status frame clears
  "off".

## 0.1.5 (unreleased)

- The time-left byte now settles when its copies count down together, which is how this washer
  reports it. A drop only counts while a state candidate reads "running".
- When more than one state byte reads "running" for the whole cycle, the add-on keeps the one that
  names the most states once a minute is left.

## 0.1.4 (unreleased)

- Calibration keeps the readings it could not settle as candidates. During the next wash the add-on
  picks the time-left copy that counts down and the state byte that reads "running", saves them,
  and adds the State sensor.

## 0.1.3 (unreleased)

- A first capture becomes the active session by itself, but only when its login is complete.
- `active_session` accepts the first characters of an id, as shown in the log.
- A first capture needs `peer_ip`, and a login with no washer id is refused before it can use the
  day's capture.

## 0.1.2 (unreleased)

- First capture works with no session in place, so a new user can record one from scratch.
- Calibration runs once per switch-on. Leaving `calibrate` on no longer steps the washer again
  after a reboot.
- The gateway closes the link on any bad frame instead of failing, and session files are checked
  before use.
- Capture refuses cloud frames with repeated keys and accepts only an IP as `cloud_addr`.
- Only plain property names become entities; a damaged offsets file no longer stops start-up.
- A cycle during calibration starts only when all its frames are recorded, and an unanswered stop
  is sent again.

## 0.1.1 (unreleased)

- Calibration reads program ids above 20, which the washer stores as a dial position.
- Calibration still finds a setting's position when the washer keeps or clamps a value
  (temperature 1, 90 °C on a 60 °C program).
- Calibration keeps a provisional time-left reading when no cycle was run; `calibrate_cycle`
  settles it.

## 0.1.0 (unreleased)

- Local gateway: login replay, heartbeat, status decoding, command replay with acknowledgement.
- MQTT bridge with Home Assistant discovery built from the recorded frames.
- Session import from capture files, guarded capture mode, calibration tool.
- Home Assistant add-on packaging.
