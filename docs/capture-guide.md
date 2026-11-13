# Capture guide

A session is the washer's login and one command frame per setting, recorded once from the vendor
cloud. After the capture the add-on serves the washer on its own, and the washer stays off the
internet.

## Why a capture is needed

Every command the cloud sends to the washer is signed, and the signing secret is not known. The
add-on cannot build a command itself, so it replays frames the real cloud produced. Under the same
login, the same setting always gives the same frame, and one recording per value is enough.

A capture lets one washer login pass through the add-on to the real cloud. The add-on records the
login replies and every command frame the cloud sends, then keeps serving that recording locally.

## Before you start

- `peer_ip` set to the address the washer's connection arrives from. With no session yet there is
  no washer id to check, so a first capture refuses to start without `peer_ip`. If you already have a session, only a login with the same
  washer id is relayed.
- Everything ready before you arm it. Once the washer has logged in to the real cloud, the session
  you had before is gone (see the warnings below). Have the driver script, its packages, your cloud
  account and the MQTT login ready first, so the capture runs in one go.
- The washer on, panel on, drum empty, door closed.
- `calibrate` off. Capture only runs in normal mode.

## 1. Redirect the washer's TLS connection to the add-on

The washer's Wi-Fi module opens one TLS connection to the vendor cloud on port 443. A router rule
must send that connection to the add-on, which listens on port 4444. Keep this rule after the
capture too: it is how the washer reaches the add-on in normal use.

In router terms you need two things:

1. A destination NAT (port forward) for traffic from the washer to any address on TCP 443, sent
   to Home Assistant on TCP 4444.
2. A source NAT (masquerade) on that redirected traffic. The washer and Home Assistant are
   usually on the same network, so without it Home Assistant answers the washer directly from its
   own address. The washer expects the answer from the cloud address and drops it. With the
   masquerade, both directions go through the router.

Example for a Linux-based router. The washer is 192.168.1.50, Home Assistant is 192.168.1.20:

```sh
iptables -t nat -I PREROUTING 1 -s 192.168.1.50 -p tcp --dport 443 \
    -j DNAT --to-destination 192.168.1.20:4444
iptables -t nat -A POSTROUTING -s 192.168.1.50 -d 192.168.1.20 -p tcp --dport 4444 \
    -j MASQUERADE
```

Because of the masquerade, the add-on sees the router's address (say 192.168.1.1) as the washer.
Set `peer_ip` to that address.

Make the rules survive a router reboot. If they vanish, the washer reaches the real cloud.

Before adding the rule, note the address the washer connects to on port 443, from the router's
connection table or DNS log. You need it as `cloud_addr` below.

## 2. Arm the capture

In the add-on options:

1. Set `cloud_addr` to the cloud's IP address you noted. Use an address, never a host name: your
   local DNS may send the name back to the add-on.
2. Set `capture_date` to today's date, `YYYY-MM-DD`, as the add-on sees it.
3. Restart the add-on.

The log shows `capture configured for <date> (armed: True)`. If it says `armed: False`, the date
does not match the add-on's clock, `cloud_addr` is empty, or today's capture has already been used.

On restart the washer reconnects. Its next login is relayed to the cloud and the log shows
`capture started: relaying this login to the cloud`. The `Mode` sensor reads `capture running`.

A capture can run once per armed day. The day is marked as used when the login is relayed, before
the cloud is even reached, so a wrong `cloud_addr` uses up that day.

## 3. Set each setting once

Every change the cloud sends produces one command frame. The add-on stores it under the label
announced just before on the MQTT topic `hisense_local/capture/next`, in the form `Property=value`
(for example `temperature=4`). A frame that arrives with no label is stored as `unlabelled_<seq>`
and never appears in Home Assistant.

While a capture runs, commands from Home Assistant answer `capture running`.

### With the driver script (recommended)

`tools/capture_drive.py` logs in to the vendor cloud API, announces each label, sets the property
and checks that the frame was stored under that label. It needs the `connectlife` and `paho-mqtt`
Python packages, and reads its settings from the environment:

| Variable | Meaning |
|---|---|
| `CL_USER`, `CL_PASSWORD` | Your vendor account |
| `MQTT_URL` | Broker the add-on uses, for example `mqtt://192.168.1.20:1883` |
| `MQTT_USER`, `MQTT_PASSWORD` | Broker login |

```sh
python3 tools/capture_drive.py --out ~/private/capture-run.json
```

It waits until the washer is online and ready, then steps through every program, temperature, spin
speed, delay from 1 to 24 hours and back to none, and each option on and off. The results file
says which steps were stored and which got no frame. Keep it outside the repository.

Add `--with-cycle` to also record start, pause, resume and stop on the quick program. The drum
really turns, so it must be empty. Without these frames Home Assistant gets no start, pause,
resume or stop buttons.

### With the vendor app

You can make each change in the vendor app instead. Before each change, publish its label to
`hisense_local/capture/next`, not retained (retained messages are ignored):

```sh
mosquitto_pub -h 192.168.1.20 -u USER -P PASSWORD -t hisense_local/capture/next -m 'temperature=4'
```

Then make that one change in the app. The add-on confirms on `hisense_local/capture/got`. A setting
that already has the value you pick sends nothing, so change away and back. The property names
and values are the ones in the add-on's catalog (`Selected_program_ID`, `temperature`,
`Spin_speed_rpm`, `DelayStartTime`, and option names such as `Prewash`).

### How a capture ends

The capture stops, keeping what it recorded, when:

- the cloud sends a second session key (one session per capture),
- the washer reconnects, or the add-on restarts,
- the cloud sends anything other than login, command, heartbeat or an empty update answer. A
  firmware offer is not passed on: the link is dropped instead.

The washer then logs in again locally on the session that was active before. On a first capture there is none until the add-on restarts, so the washer is turned away until then.

## 4. Finish

1. Clear `capture_date` and `cloud_addr`, then restart the add-on.
2. The log shows `capture closed, session abcd…, N frames`. On a first capture the new session is
   made active right away and the log says so.
3. If another session was active, set `active_session` to the four characters from that log line
   (any unique start of the id works) and restart.
4. Block the washer from the internet on the router, keeping the redirect. For example, drop
   anything from the washer that leaves the local network:

   ```sh
   iptables -I FORWARD 1 -s 192.168.1.50 ! -d 192.168.1.0/24 -j DROP
   ```

   The redirect is applied before this rule, so the washer still reaches the add-on.

Then run a calibration (see the README) so the status sensors appear.

## Warnings

- Never let the washer log in to the real cloud again after a capture. Each cloud login rotates
  the session key, the recorded frames stop working, and the whole session has to be captured
  again. Keep the redirect and the internet block in place.
- Keep session files private. Anyone with a session file can drive your washer from your network.
  The add-on writes them with mode 0600; keep any copy outside the repository and off shared
  folders.
- Firmware offers are blocked during a capture, and the washer gets no updates afterwards. See
  [limits.md](limits.md).
- The cloud's certificate is not checked during a capture. It does not match its name, and the
  washer does not check it either. Keep the capture window short.
