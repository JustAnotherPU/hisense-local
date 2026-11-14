"""Process wiring for the add-on: run (gateway + MQTT) and calibrate (gateway only)."""
import asyncio
import json
import logging
import os
import signal
import time
from urllib.parse import urlparse

from . import catalog
from . import session as sessionlib
from . import wire
from .bridge import BASE, NAME, VERSION, Bridge
from .gateway import Gateway, server_context, short

log = logging.getLogger("app")
PORT = 4444


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def load_offsets(path):
    """The calibration result, minus anything that would not read cleanly. Never raises."""
    try:
        raw = load_json(path, {}).get("offsets", {})
        good = {k: v for k, v in raw.items() if NAME.fullmatch(k) and wire.valid_spec(v)}
    except (ValueError, AttributeError, OSError, RecursionError) as exc:
        log.error("status_offsets.json is unreadable (%s): status sensors are off", type(exc).__name__)
        return {}
    if len(good) != len(raw):
        log.warning("status_offsets.json: %d malformed entries ignored", len(raw) - len(good))
    return good


def pick_offsets(path, model):
    """The calibration result, or the built-in readings for a tested model when none exists."""
    offsets = load_offsets(path)
    if not offsets and model in catalog.PROFILES:
        log.info("no calibration yet: using the built-in %s readings", model)
        offsets = {k: v for k, v in catalog.PROFILES[model].items() if wire.valid_spec(v)}
    return offsets


def open_learner(path, share, offsets):
    """A Learner for what calibration left open, or None. Never raises."""
    from . import calibrate
    try:
        saved = load_json(path, {})
        candidates = saved.get("candidates")
        if candidates is None:
            # written before candidates were kept: rebuild them from the matching report
            report = load_json(os.path.join(share, "calibration.json"), {})
            if report.get("started") != saved.get("at"):
                return None
            # shortcut: a field the rebuild settles outright is not adopted; recalibrate to get it
            candidates = calibrate.derive(report.get("steps", []), report.get("timeline", []))[1]
        clock = offsets.get("minutes_left")
        learner = calibrate.Learner(candidates, clock=clock if type(clock) is int else None)
    except Exception as exc:  # a bad file only means nothing to learn
        log.warning("calibration candidates unreadable (%s): nothing to learn", type(exc).__name__)
        return None
    left = learner.remaining()
    if left:
        log.info("will settle during the next cycle: %s", ", ".join(sorted(left)))
    return learner if left else None


def quiet_tls_errors(loop, context):
    exc = context.get("exception")
    log.warning("%s (%s: %s)", context.get("message"), type(exc).__name__ if exc else "-", exc or "")


def open_session(args, opts):
    data = sessionlib.DataDir(args.data)
    try:
        sid = data.adopt_import(os.path.join(args.share, "import", "session.json"))
        if sid:
            log.info("imported session %s (shared copy deleted)", short(sid))
    except (sessionlib.SessionError, OSError) as exc:
        log.error("import failed: %s (shared copy deleted)", exc if isinstance(exc, sessionlib.SessionError)
                  else type(exc).__name__)
    try:
        session = data.load_active(opts.get("active_session", ""))
    except sessionlib.SessionError as exc:
        log.error("active session: %s", exc)
        session = None
    if session:
        log.info("session %s active, %d frames", short(session.id), len(session.labels))
    else:
        log.warning("no active session: the washer is turned away unless a capture is armed")
    return data, session


async def stop_event():
    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, event.set)
    return event


def mqtt_client(opts):
    import paho.mqtt.client as mqtt

    url = urlparse(opts.get("mqtt_url") or "mqtt://core-mosquitto:1883")
    extra = {"callback_api_version": mqtt.CallbackAPIVersion.VERSION2} if hasattr(mqtt, "CallbackAPIVersion") else {}
    client = mqtt.Client(client_id="hisense_local", clean_session=True, **extra)
    if opts.get("mqtt_user"):
        client.username_pw_set(opts["mqtt_user"], opts.get("mqtt_password") or None)
    if url.scheme == "mqtts":
        client.tls_set()
    client.will_set(f"{BASE}/alive", "offline", qos=1, retain=True)
    return client, url.hostname, url.port or (8883 if url.scheme == "mqtts" else 1883)


async def serve(args, opts, with_mqtt):
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(quiet_tls_errors)
    log.info("hisense-local %s", VERSION)
    data, session = open_session(args, opts)

    capture = None
    if with_mqtt and opts.get("capture_date"):
        from .capture import Capture  # loaded only when the owner arms a capture

        capture = Capture(data, opts["capture_date"], opts.get("cloud_addr", ""))
        log.warning("capture configured for %s (armed: %s)", opts["capture_date"], capture.armed())

    if not opts.get("peer_ip"):
        log.warning("peer_ip is empty: any device that reaches port %d and knows the washer id "
                    "can log in as the washer", PORT)
    gw = Gateway(session, peer_ip=opts.get("peer_ip", ""), capture=capture)
    tls = server_context(os.path.join(args.tls, "cert.pem"), os.path.join(args.tls, "key.pem"))
    server = await gw.serve(PORT, tls)
    log.info("listening on %d", PORT)
    return data, gw, server, capture


async def run_async(args, opts):
    data, gw, server, capture = await serve(args, opts, with_mqtt=True)
    client, host, port = mqtt_client(opts)
    path = os.path.join(args.data, "status_offsets.json")
    offsets = pick_offsets(path, opts.get("model", ""))
    learner = open_learner(path, args.share, offsets)

    def save_offsets(found, left):
        sessionlib.write_private(path, {"offsets": found, "candidates": left, "at": time.time()})

    bridge = Bridge(client, asyncio.get_running_loop(), gw, offsets=offsets,
                    per_entity=opts.get("discovery") == "per_entity", capture=capture,
                    learner=learner, save_offsets=save_offsets)
    gw.on_status, gw.on_link = bridge.on_status, bridge.on_link
    if capture is not None:
        capture.on_got = bridge.capture_got
    client.on_connect, client.on_message = bridge.on_connect, bridge.on_message
    client.connect_async(host, port)
    client.loop_start()
    try:
        await (await stop_event()).wait()
    finally:
        server.close()
        client.publish(f"{BASE}/alive", "offline", qos=1, retain=True)
        client.disconnect()
        client.loop_stop()
    return 0


async def calibrate_async(args, opts):
    from . import calibrate

    data, gw, server, _ = await serve(args, opts, with_mqtt=False)
    if gw.session is None:
        return 1
    latest = []
    gw.on_status = lambda values: latest.append(values)
    log.info("calibration: waiting for the washer (turn it on, panel on, drum empty)")
    for _ in range(600):
        if gw.ready and latest:
            break
        await asyncio.sleep(1)
    else:
        log.error("washer did not log in within 10 minutes")
        return 1
    task = asyncio.ensure_future(calibrate.run(gw, lambda: latest[-1] if latest else None,
                                               with_cycle=args.with_cycle))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # stopping the add-on still restores the washer
        loop.add_signal_handler(sig, task.cancel)
    try:
        report = await task
    except asyncio.CancelledError:
        log.warning("calibration interrupted; settings restored, no offsets written")
        return 1
    finally:
        server.close()
    report["session"] = gw.session.id[:4]
    os.makedirs(args.share, exist_ok=True)
    out = os.path.join(args.share, "calibration.json")
    sessionlib.write_private(out, report)
    sessionlib.write_private(os.path.join(args.data, "status_offsets.json"),
                             {"offsets": report["offsets"], "candidates": report["ambiguous"],
                              "at": report["started"]})
    log.info("offsets found: %s; ambiguous: %s; report: %s",
             report["offsets"], report["ambiguous"], out)
    return 0


def run(args):
    opts = load_json(args.options, {})
    # one run per switch-on: a reboot with calibrate left on must not step the washer again
    done = os.path.join(args.data, "calibration.done")
    if opts.get("calibrate"):  # add-on option: calibrate once instead of serving
        if os.path.exists(done):
            log.warning("calibration already ran: turn calibrate off, restart, then on to run it again")
        else:
            sessionlib.write_private(done, {"at": time.time()})
            args.with_cycle = bool(opts.get("calibrate_cycle"))
            return asyncio.run(calibrate_async(args, opts))
    elif os.path.exists(done):
        os.remove(done)
    return asyncio.run(run_async(args, opts))


def calibrate(args):
    return asyncio.run(calibrate_async(args, load_json(args.options, {})))
