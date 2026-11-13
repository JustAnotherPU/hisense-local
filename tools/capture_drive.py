"""Drive a capture session: set each property through the vendor cloud API and label its frame.

Before running: the add-on has capture_date set to today, the washer is on with the
panel on, drum empty, door closed. Every API call makes the cloud push one 201 frame
through the add-on, which stores it under the label announced on capture/next.

Needs the `connectlife` and `paho-mqtt` packages. Credentials come from the
environment: CL_USER, CL_PASSWORD, MQTT_URL, MQTT_USER, MQTT_PASSWORD.

  python tools/capture_drive.py --out ~/private/capture-run.json [--with-cycle]
"""
import argparse
import asyncio
import json
import os
import queue
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon"))
from hisense_local import catalog  # noqa: E402

BASE = "hisense_local"


def steps(with_cycle):
    out = [("Selected_program_ID", p) for p in catalog.PROGRAMS]
    out.append(("Selected_program_ID", 8))
    out += [("temperature", t) for t in catalog.TEMPERATURES]
    out += [("Spin_speed_rpm", s) for s in sorted(catalog.SPINS)]
    out += [("DelayStartTime", h) for h in range(1, 25)] + [("DelayStartTime", 0)]
    out.append(("Selected_program_ID", 42))
    for prop in catalog.SWITCH_NAMES:
        out += [(prop, 1), (prop, 0)]
    if with_cycle:
        out += [("Selected_program_ID", catalog.QUICK_PROGRAM), ("Actions", 2), ("Remote_hold", 1),
                ("Remote_hold", 0), ("Actions", 1)]
    return out


def mqtt_listener():
    import paho.mqtt.client as mqtt

    url = urlparse(os.environ.get("MQTT_URL", "mqtt://localhost:1883"))
    extra = {"callback_api_version": mqtt.CallbackAPIVersion.VERSION2} if hasattr(mqtt, "CallbackAPIVersion") else {}
    client = mqtt.Client(client_id="hisense_local_drive", clean_session=True, **extra)
    if os.environ.get("MQTT_USER"):
        client.username_pw_set(os.environ["MQTT_USER"], os.environ.get("MQTT_PASSWORD"))
    got = queue.Queue()
    client.on_connect = lambda c, u, f, rc, p=None: c.subscribe(f"{BASE}/capture/got", qos=0)
    client.on_message = lambda c, u, m: got.put(json.loads(m.payload))
    client.connect(url.hostname, url.port or 1883)
    client.loop_start()
    return client, got


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="results file, keep it outside the repo")
    parser.add_argument("--with-cycle", action="store_true", help="also START/PAUSE/RESUME/STOP")
    parser.add_argument("--wait", type=float, default=10.0)
    args = parser.parse_args()

    from connectlife.api import ConnectLifeApi

    client, got = mqtt_listener()
    api = ConnectLifeApi(os.environ["CL_USER"], os.environ["CL_PASSWORD"])
    await api.login()
    washer = next(a for a in await api.get_appliances() if a.device_type_code == "025")
    for _ in range(120):
        if washer.offline_state == 1 and washer.status_list.get("machine_status") == 1:
            break
        print("waiting for the washer to be on and ready...")
        await asyncio.sleep(5)
        washer = next(a for a in await api.get_appliances() if a.puid == washer.puid)
    else:
        raise SystemExit("washer never became ready")

    results = []
    for prop, value in steps(args.with_cycle):
        label = f"{prop}={value}"
        client.publish(f"{BASE}/capture/next", label, qos=0, retain=False)
        await asyncio.sleep(0.3)
        record = {"label": label, "t": time.time()}
        try:
            await api.update_appliance(washer.puid, {prop: str(value)})
            answer = await asyncio.to_thread(got.get, True, args.wait)
            record["seq"] = answer.get("seq")
            if answer.get("label") != label:
                raise SystemExit(f"frame stored as {answer.get('label')} while expecting {label}: stop")
        except queue.Empty:
            record["error"] = "no frame (refused or no-op)"
        except Exception as exc:  # API refusals carry the reason in the message
            record["error"] = str(exc)[-120:]
        results.append(record)
        print(label, record.get("error", "stored"))
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=1)
        os.chmod(args.out, 0o600)
    client.loop_stop()


if __name__ == "__main__":
    asyncio.run(main())
