"""Ingress panel: a small page inside Home Assistant, no terminal, for non-technical users.

Shows whether the washer is connected, how many commands it has learned, and which mode
it is in, with one button to switch between learning (relay to the cloud, record what you
do in the app) and local control (serve offline). Served by a stdlib HTTP server in a
daemon thread so it adds no dependency; Supervisor proxies it as the add-on's Ingress UI.

The mode switch writes the add-on's own option and restarts it through the Supervisor API
(the add-on may change and restart itself). Read endpoints only read shared state.
"""
import json
import logging
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("web")

SUPERVISOR = "http://supervisor"


def build_status(state, washer_online, learn_on):
    """UI payload from the bridge state dict + two live flags. Pure, so it is testable."""
    commands = state.get("frames", 0) if isinstance(state, dict) else 0
    if learn_on:
        mode, headline = "learn", ("Learning from your washer" if washer_online
                                   else "Waiting for your washer")
    elif commands:
        mode, headline = "local", ("Local control active" if washer_online
                                   else "Local control (washer offline)")
    else:
        mode, headline = "empty", "Nothing learned yet"
    return {
        "headline": headline,
        "mode": mode,
        "online": bool(washer_online),
        "commands": commands,
        "machine_status": state.get("machine_status") if isinstance(state, dict) else None,
        "session": state.get("session") if isinstance(state, dict) else None,
    }


def set_own_option(name, value):
    """Set one of this add-on's options and restart it, via the Supervisor API."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("no SUPERVISOR_TOKEN: cannot change options")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    info = _api("GET", "/addons/self/info", headers)
    options = dict(info["data"]["options"])
    options[name] = value
    _api("POST", "/addons/self/options", headers, {"options": options})
    _api("POST", "/addons/self/restart", headers, {})


def _api(method, path, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SUPERVISOR + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read() or b"{}")


class _Handler(BaseHTTPRequestHandler):
    status_getter = staticmethod(lambda: {})
    set_mode = staticmethod(lambda learn_on: None)

    def log_message(self, *_):  # keep the add-on log quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        payload = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, json.dumps(self.status_getter()))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/api/mode":
            return self._send(404, json.dumps({"error": "not found"}))
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            learn_on = bool(body.get("mode") == "learn")
            self.set_mode(learn_on)
        except Exception as exc:  # a bad request or a Supervisor error: report, do not crash
            return self._send(500, json.dumps({"error": type(exc).__name__}))
        self._send(200, json.dumps({"ok": True, "restarting": True}))


def serve_ingress(port, status_getter, set_mode):
    """Start the Ingress HTTP server in a daemon thread. Returns the server (for .shutdown())."""
    _Handler.status_getter = staticmethod(status_getter)
    _Handler.set_mode = staticmethod(set_mode)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("Ingress panel on :%d", port)
    return httpd


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hisense Local</title>
<style>
  :root { --bg:#f6f7f9; --fg:#1b1d21; --muted:#6b7280; --line:#e5e7eb;
          --go:#1f7a4d; --go-fg:#fff; --dot:#9ca3af; --dot-on:#1f7a4d; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16181d; --fg:#eef0f3; --muted:#9aa1ab; --line:#272b32;
            --go:#2e9d66; --go-fg:#07140d; --dot:#4b5563; --dot-on:#34d07f; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         min-height:100vh; display:flex; justify-content:center; }
  main { width:100%; max-width:460px; padding:40px 24px; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--dot);
         display:inline-block; margin-right:8px; transition:background .3s; }
  .dot.on { background:var(--dot-on); }
  h1 { font-size:15px; font-weight:600; color:var(--muted); letter-spacing:.02em;
       text-transform:uppercase; margin:0 0 28px; }
  .headline { font-size:26px; font-weight:650; letter-spacing:-.01em; margin:0 0 6px; }
  .sub { color:var(--muted); margin:0 0 36px; }
  .count { font-size:54px; font-weight:700; letter-spacing:-.02em; line-height:1; }
  .count small { font-size:16px; font-weight:500; color:var(--muted); letter-spacing:0; }
  button { width:100%; border:0; border-radius:14px; padding:16px; font-size:17px;
           font-weight:600; background:var(--go); color:var(--go-fg); cursor:pointer;
           transition:opacity .15s, transform .05s; margin-top:36px; }
  button:disabled { opacity:.5; cursor:default; }
  button:active:not(:disabled) { transform:scale(.99); }
  .note { font-size:14px; color:var(--muted); margin-top:20px; }
  .err { color:#c0392b; }
</style></head>
<body><main>
  <h1>Hisense Local</h1>
  <p class="headline"><span id="dot" class="dot"></span><span id="headline">Loading…</span></p>
  <p class="sub" id="sub">Checking your washer</p>
  <div class="count"><span id="count">0</span> <small>commands learned</small></div>
  <button id="btn" disabled>…</button>
  <p class="note" id="note"></p>
</main>
<script>
const $ = id => document.getElementById(id);
let state = null;

async function refresh() {
  try {
    const r = await fetch('api/status', {cache:'no-store'});
    state = await r.json();
    render();
  } catch (e) { $('sub').textContent = 'Panel offline — reconnecting…'; }
}

function render() {
  if (!state) return;
  $('headline').textContent = state.headline;
  $('dot').className = 'dot' + (state.online ? ' on' : '');
  $('count').textContent = state.commands;
  const learning = state.mode === 'learn';
  $('sub').textContent = learning
    ? 'Use your washer from the Hisense app — each setting you pick is learned here.'
    : (state.commands ? 'Your washer is controlled locally, no cloud needed.'
                      : 'Turn on learning, then use the app once to teach it.');
  const btn = $('btn');
  btn.disabled = false;
  btn.textContent = learning ? 'Switch to local control' : 'Start learning';
  $('note').textContent = '';
}

$('btn').onclick = async () => {
  const learning = state && state.mode === 'learn';
  $('btn').disabled = true;
  $('note').textContent = 'Saving and restarting…';
  try {
    await fetch('api/mode', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode: learning ? 'local' : 'learn'})});
    $('note').textContent = 'Restarting — this page will reconnect in a few seconds.';
    setTimeout(refresh, 8000);
  } catch (e) {
    $('note').className = 'note err';
    $('note').textContent = 'Could not switch mode.';
    $('btn').disabled = false;
  }
};

refresh();
setInterval(refresh, 5000);
</script>
</body></html>"""


# ---- offline self-check --------------------------------------------------------------

def _selftest():
    s = build_status({"frames": 12, "session": "a1b2", "machine_status": "idle"},
                     washer_online=True, learn_on=True)
    assert s["mode"] == "learn" and s["commands"] == 12 and s["online"] is True
    assert "Learning" in s["headline"]

    s = build_status({"frames": 12}, washer_online=False, learn_on=True)
    assert "Waiting" in s["headline"]

    s = build_status({"frames": 8}, washer_online=True, learn_on=False)
    assert s["mode"] == "local" and "Local control active" == s["headline"]

    s = build_status({"frames": 0}, washer_online=False, learn_on=False)
    assert s["mode"] == "empty" and "Nothing learned" in s["headline"]

    s = build_status({}, washer_online=False, learn_on=False)
    assert s["commands"] == 0 and s["mode"] == "empty"
    print("web selftest OK")


if __name__ == "__main__":
    _selftest()
