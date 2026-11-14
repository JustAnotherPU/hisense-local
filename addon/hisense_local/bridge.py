"""MQTT side: Home Assistant discovery, one state topic, command routing.

The MQTT client runs in its own thread; commands are handed to the asyncio
loop with run_coroutine_threadsafe. Retained messages on command topics are
dropped, so a stale message on the broker can never start the washer.
"""
import asyncio
import json
import logging
import re

from . import catalog, wire

log = logging.getLogger("bridge")

BASE = "hisense_local"
DEVICE_ID = "hisense_local"
VERSION = "0.1.8"
# property and field names end up in MQTT topics and Home Assistant templates
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def controls(labels):
    """Group recorded labels into the controls they allow: {prop: set(values)}."""
    out = {}
    for label in labels:
        prop, _, value = label.partition("=")
        if NAME.fullmatch(prop) and value.lstrip("-").isdigit():
            out.setdefault(prop, set()).add(int(value))
    return out


class Bridge:
    def __init__(self, client, loop, gateway, *, offsets=None, per_entity=False,
                 mode="local", capture=None, learner=None, save_offsets=None):
        self.client = client
        self.loop = loop
        self.gateway = gateway
        self.offsets = dict(catalog.STATUS_OFFSETS, **(offsets or {}))
        self.per_entity = per_entity
        self.mode = mode
        self.capture = capture
        self.learner, self.save_offsets = learner, save_offsets
        self.values = None  # last decoded status
        self.acked = {}  # prop -> value, from acknowledged commands
        self.last_result = None
        self.asleep = False  # a command went unanswered: the washer switched itself off
        self.available = controls(gateway.session.labels if gateway.session else ())
        self.selects, self.switches, self.buttons = self._plan()
        self.offered = {f"{p}={v}" for p, (_, names) in self.selects.items() for v in names}
        self.offered |= {f"{p}={v}" for p in self.switches for v in (0, 1)}
        self.offered |= {f"{p}={v}" for p, v in self.buttons.values()}

    # -- what to expose ------------------------------------------------------------
    def _plan(self):
        selects = {}
        for prop, (key, names) in catalog.SELECTS.items():
            values = sorted(v for v in self.available.get(prop, ()) if v in names)
            if values:
                selects[prop] = (key, {v: names[v] for v in values})
        button_props = {p for p, _ in catalog.BUTTONS.values()}
        switches = sorted(p for p, vals in self.available.items()
                          if vals >= {0, 1} and len(vals) == 2 and p not in button_props
                          and p not in catalog.SELECTS and p not in catalog.NOT_EXPOSED)
        buttons = {k: pv for k, pv in catalog.BUTTONS.items() if pv[1] in self.available.get(pv[0], ())}
        return selects, switches, buttons

    def components(self):
        both = {"avty": [{"t": f"{BASE}/alive"}, {"t": f"{BASE}/washer"}], "avty_mode": "all"}
        cmps = {}
        for prop, (key, names) in self.selects.items():
            cmps[key] = dict(both, p="select", name=catalog.ENTITY_NAMES[key], cmd_t=f"{BASE}/set/{prop}",
                             options=list(names.values()), val_tpl=f"{{{{ value_json.{prop} }}}}")
        for prop in self.switches:
            cmps[prop.lower()] = dict(both, p="switch", name=catalog.SWITCH_NAMES.get(prop, prop), cmd_t=f"{BASE}/set/{prop}",
                                      pl_on="1", pl_off="0", stat_on="ON", stat_off="OFF",
                                      val_tpl=f"{{{{ value_json.{prop} }}}}")
        for key, (prop, value) in self.buttons.items():
            cmps[key] = dict(both, p="button", name=catalog.ENTITY_NAMES[key], cmd_t=f"{BASE}/set/{prop}",
                             pl_prs=str(value))
        for field in self.offsets:
            if field in self.available:
                continue  # already shown by its control
            label, unit = catalog.SENSOR_NAMES.get(field, (field, None))
            cmps[field] = dict(both, p="sensor", name=label,
                               val_tpl=f"{{{{ value_json.{field} }}}}",
                               **({"unit_of_meas": unit} if unit else {}))
        for key in ("last_result", "frames", "session", "mode"):
            cmps[key] = {"p": "sensor", "name": catalog.ENTITY_NAMES[key], "ent_cat": "diagnostic",
                         "val_tpl": f"{{{{ value_json.{key} }}}}"}
        for key, cmp in cmps.items():
            cmp["uniq_id"] = f"{DEVICE_ID}_{key}"
        return cmps

    def discovery(self):
        """[(topic, payload)] for the retained discovery messages."""
        device = {"ids": [DEVICE_ID], "name": "Washer",
                  "mf": "Hisense", "mdl": "WF5I1045BWQ"}
        origin = {"name": "hisense-local", "sw": VERSION}
        top = {"avty": [{"t": f"{BASE}/alive"}], "stat_t": f"{BASE}/state"}
        cmps = self.components()
        if not self.per_entity:
            return [(f"homeassistant/device/{DEVICE_ID}/config",
                     dict(top, dev=device, o=origin, cmps=cmps))]
        out = []
        for key, cmp in cmps.items():
            cmp = dict(cmp)
            platform = cmp.pop("p")
            payload = dict(top, dev=device, o=origin, **cmp)
            out.append((f"homeassistant/{platform}/{DEVICE_ID}/{key}/config", payload))
        return out

    # -- MQTT callbacks (client thread) --------------------------------------------
    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            log.error("MQTT connection refused (%s)", rc)
            return
        log.info("MQTT connected")
        client.subscribe(f"{BASE}/set/+", qos=0)
        client.subscribe(f"{BASE}/capture/next", qos=0)
        self.publish_discovery()
        client.publish(f"{BASE}/alive", "online", qos=1, retain=True)
        self.loop.call_soon_threadsafe(self.publish_link)

    def publish_discovery(self):
        for topic, payload in self.discovery():
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)

    def on_message(self, client, userdata, msg):
        if msg.retain:
            log.warning("ignored a retained message on %s", msg.topic)
            return
        payload = msg.payload[:128].decode("utf-8", "replace").strip()  # it is echoed in the state
        if msg.topic == f"{BASE}/capture/next":
            if self.capture is not None:
                self.loop.call_soon_threadsafe(setattr, self.capture, "next_label", payload)
            return
        prop = msg.topic.rsplit("/", 1)[-1]
        asyncio.run_coroutine_threadsafe(self.command(prop, payload), self.loop)

    # -- loop side -----------------------------------------------------------------
    def label_for(self, prop, payload):
        if prop in self.selects:
            for value, text in self.selects[prop][1].items():
                if text == payload:
                    return f"{prop}={value}"
        if payload in ("ON", "OFF"):
            payload = "1" if payload == "ON" else "0"
        return f"{prop}={payload}"

    async def command(self, prop, payload):
        label = self.label_for(prop, payload)
        # only what discovery offers can be sent (temperature=1, dose levels... stay out)
        result = await self.gateway.send(label) if label in self.offered else "not offered"
        log.info("command %s -> %s", label, result)
        if result in ("ok", "no ack"):
            self.asleep = result == "no ack"
        if result == "ok":
            prop, _, value = label.partition("=")
            self.acked[prop] = int(value) if value.lstrip("-").isdigit() else value
        self.last_result = f"{label}: {result}"
        self.client.publish(f"{BASE}/result", json.dumps({"label": label, "result": result}), qos=0)
        self.publish_state()  # a refused change snaps back in the UI
        return result

    def on_status(self, values):
        self.values, self.asleep = values, False
        got = self.learner.feed(values) if self.learner else {}
        if got:
            log.info("learned from this cycle: %s", got)
            self.offsets.update(got)
            if self.save_offsets:
                self.save_offsets(self.offsets, self.learner.remaining())
            self.publish_discovery()  # a new sensor may appear
        self.publish_state()

    def on_link(self, ready):
        if not ready:
            self.acked.clear()  # ack-based state is unknown after a reconnect
        self.publish_link()

    def publish_link(self):
        self.client.publish(f"{BASE}/washer", "online" if self.gateway.ready else "offline",
                            qos=1, retain=True)
        self.publish_state()

    def current(self, prop):
        spec = self.offsets.get(prop)
        if spec is not None and self.values is not None:
            value = wire.field_value(self.values, spec)
            if value is not None:
                return value
        return self.acked.get(prop)

    def state(self):
        out = {}
        for prop, (_, names) in self.selects.items():
            out[prop] = names.get(self.current(prop))
        for prop in self.switches:
            value = self.current(prop)
            out[prop] = None if value is None else ("ON" if value else "OFF")
        for field, spec in self.offsets.items():
            if field not in out and self.values is not None:
                out[field] = wire.field_value(self.values, spec)
        if "machine_status" in out and not isinstance(out["machine_status"], str):
            out["machine_status"] = None  # a value no cycle has named yet
        if out.get("machine_status") == "running" and out.get("minutes_left") == 0:
            out["machine_status"] = "done"  # the end screen keeps the running value
        if self.asleep and "machine_status" in out:
            out["machine_status"] = "off"
        session = self.gateway.session
        mode = self.mode
        if self.capture is not None:
            mode = "capture running" if self.capture.running else (
                "capture armed" if self.capture.armed() else mode)
        out.update(last_result=self.last_result, frames=len(session.labels) if session else 0,
                   session=session.id[:4] if session else None, mode=mode)
        return out

    def publish_state(self):
        self.client.publish(f"{BASE}/state", json.dumps(self.state()), qos=0, retain=True)

    def capture_got(self, label, seq, sid):
        self.client.publish(f"{BASE}/capture/got",
                            json.dumps({"label": label, "seq": seq, "session": sid[:4] if sid else None}),
                            qos=0)
