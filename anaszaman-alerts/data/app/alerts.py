"""Umbrel Alerts: watches this Umbrel through its MCP endpoint and messages you on WhatsApp.

Runs as an umbrelOS app. Config and state live in /state (the app's data folder).
WhatsApp: the Agent Platform API (https://api.whatsapp.com/agent/v1); an agent can only
message the person who created it, and learns who that is from the agent's own history.
"""
import html
import json
import os
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import rules

STATE_DIR = os.environ.get("STATE_DIR", "/state")
EXTERNAL = os.environ.get("EXTERNAL_DIR", "/external")
MCP_URL = os.environ.get("MCP_URL", "http://10.21.0.1/mcp")
AGENT_API = "https://api.whatsapp.com/agent/v1"
INTERVAL = 60
CONFIG = os.path.join(STATE_DIR, "config.json")
STATE = os.path.join(STATE_DIR, "state.json")

lock = threading.Lock()
status = {"last_check": None, "last_error": None, "last_sent": None, "recipient": None}


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# ---------------- Umbrel (MCP) ----------------

def mcp(tool, args=None, token=""):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args or {}}}).encode()
    req = urllib.request.Request(MCP_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + token.removeprefix("Bearer ").strip(),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
    payload = "".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:")) or raw
    msg = json.loads(payload)
    if "error" in msg:
        raise RuntimeError(msg["error"].get("message", "MCP error"))
    res = msg.get("result", {})
    text = (res.get("content") or [{}])[0].get("text", "")
    if res.get("isError"):
        raise RuntimeError(text or "MCP tool error")
    try:
        return json.loads(text)
    except Exception:
        return text


def snapshot(token):
    apps = mcp("list_apps", token=token)
    res = mcp("get_system_resources", token=token)
    info = mcp("get_system_info", token=token)
    try:
        notes = mcp("get_notifications", token=token)
    except Exception:
        notes = []
    note_texts = []
    for n in notes if isinstance(notes, list) else []:
        if isinstance(n, str):
            note_texts.append(n)
        elif isinstance(n, dict):
            note_texts.append(next((str(n[k]) for k in ("message", "title", "text", "body") if n.get(k)), json.dumps(n)))
    storage, memory, temp = res.get("storage", {}), res.get("memory", {}), res.get("temperature", {})
    disks = {"Internal SSD": (storage.get("totalUsed", 0), storage.get("size", 0))}
    mounts = []
    if os.path.isdir(EXTERNAL):
        for name in sorted(os.listdir(EXTERNAL)):
            path = os.path.join(EXTERNAL, name)
            if os.path.ismount(path):
                mounts.append(name)
                u = shutil.disk_usage(path)
                disks[f"Drive “{name}”"] = (u.used, u.total)
    return {
        "apps": {a["id"]: a.get("state", "") for a in apps},
        "names": {a["id"]: a.get("name", a["id"]) for a in apps},
        "updates": [a["id"] for a in apps if a.get("updateAvailable")],
        "uptime": info.get("uptimeSeconds"),
        "disks": disks,
        "mounts": mounts,
        "temp": temp.get("temperature"),
        "temp_warning": temp.get("warning", "normal"),
        "memory_fraction": (memory.get("totalUsed", 0) / memory["size"]) if memory.get("size") else 0,
        "notifications": note_texts,
    }


# ---------------- WhatsApp agent ----------------

def wa(path, key, body=None, timeout=15):
    req = urllib.request.Request(AGENT_API + path, method="POST" if body else "GET",
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": "Bearer " + key.strip(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read().decode()
            return r.status, (json.loads(data) if data else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def recipient(key, state):
    """The agent's creator, read from its history (you must have messaged it once)."""
    if state.get("recipient"):
        return state["recipient"]
    code, data = wa("/updates?offset=0&limit=100&timeout=0", key)
    if code != 200 or not data:
        return None
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            v = change.get("value", {})
            frm = next((m["from"] for m in v.get("messages", []) if str(m.get("from", "")).startswith("user:")), None)
            to = (v.get("statuses") or [{}])[0].get("recipient_id")
            if frm or to:
                state["recipient"] = frm or to
                return state["recipient"]
    return None


def send(key, to, text):
    code, _ = wa("/messages", key, {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096]}})
    return 200 <= code < 300, code


# ---------------- Loop ----------------

def check_once():
    cfg, state = load(CONFIG, {}), load(STATE, {})
    key, token = cfg.get("whatsapp_key", ""), cfg.get("mcp_token", "")
    if not key or not token:
        status["last_error"] = "Add the WhatsApp agent key and MCP token on this page."
        return
    now = snapshot(token)
    alerts, nxt = rules.evaluate(state.get("rules", {}), now, time.time())
    outbox = state.get("outbox", []) + ([rules.format_alerts(alerts)] if alerts else [])
    to = recipient(key, state)
    status["recipient"] = to
    if outbox and to:
        sent = []
        for msg in outbox[:3]:  # stay well under 12 sends/min
            ok, code = send(key, to, msg)
            if not ok:
                status["last_error"] = f"WhatsApp answered {code}; will retry next minute."
                break
            sent.append(msg)
            status["last_sent"] = time.strftime("%Y-%m-%d %H:%M")
        outbox = outbox[len(sent):]
    elif outbox and not to:
        status["last_error"] = "Send any message to your WhatsApp agent once so it knows who to alert."
    state.update(rules=nxt, outbox=outbox[-50:])
    save(STATE, state)
    status["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if to and not outbox:
        status["last_error"] = None


def loop():
    while True:
        try:
            with lock:
                check_once()
        except Exception as e:
            status["last_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        time.sleep(INTERVAL)


# ---------------- Setup page ----------------

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Umbrel Alerts</title><style>
:root{{color-scheme:light dark;--bg:#fff;--ink:#1d1d1f;--muted:#6e6e73;--panel:#f5f5f7;--line:#d2d2d7;--accent:#2d4fa3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--ink:#f5f5f7;--muted:#98989d;--panel:#1c1c1e;--line:#38383a;--accent:#8aa4ff}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
main{{max-width:560px;margin:0 auto;padding:40px 16px}} h1{{font-size:28px;margin:0 0 4px}} p.sub{{color:var(--muted);margin:0 0 28px}}
.card{{background:var(--panel);border-radius:14px;padding:16px;margin-bottom:16px}} .row{{display:flex;justify-content:space-between;gap:12px;padding:6px 0}}
.row span:first-child{{color:var(--muted)}} label{{display:block;font-weight:600;margin:12px 0 6px}} small{{color:var(--muted)}}
input{{width:100%;box-sizing:border-box;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit}}
button{{margin-top:16px;padding:10px 18px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}}
button.ghost{{background:transparent;color:var(--accent);border:1px solid var(--line)}} .ok{{color:#34c759}} .bad{{color:#ff9f0a}}
</style></head><body><main>
<h1>Umbrel Alerts</h1><p class="sub">Messages you on WhatsApp when something on this Umbrel needs attention.</p>
<div class="card">
<div class="row"><span>Status</span><span class="{cls}">{state}</span></div>
<div class="row"><span>Last check</span><span>{last_check}</span></div>
<div class="row"><span>Last alert sent</span><span>{last_sent}</span></div>
<div class="row"><span>WhatsApp</span><span>{who}</span></div>
</div>
{notice}
<form class="card" method="post" action="config">
<label>WhatsApp agent API key</label><input name="whatsapp_key" type="password" placeholder="{key_ph}" autocomplete="off">
<small>From the WhatsApp agent you created. Message the agent once (any text) so it knows who you are.</small>
<label>Umbrel MCP token</label><input name="mcp_token" type="password" placeholder="{tok_ph}" autocomplete="off">
<small>Umbrel → Settings → AI agents (MCP) → Connect a new agent. Read access is enough.</small>
<br><button>Save</button></form>
<form method="post" action="test"><button class="ghost">Send test alert</button></form>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def page(self, notice=""):
        cfg = load(CONFIG, {})
        healthy = status["last_check"] and not status["last_error"]
        body = PAGE.format(
            cls="ok" if healthy else "bad",
            state="Watching" if healthy else html.escape(status["last_error"] or "Starting…"),
            last_check=html.escape(status["last_check"] or "–"),
            last_sent=html.escape(status["last_sent"] or "–"),
            who="Connected" if status["recipient"] else "Waiting for your first message to the agent",
            notice=f'<div class="card">{html.escape(notice)}</div>' if notice else "",
            key_ph="Saved (leave empty to keep)" if cfg.get("whatsapp_key") else "Paste the agent key",
            tok_ph="Saved (leave empty to keep)" if cfg.get("mcp_token") else "umbrelmcp_…",
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.page()

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
        notice = ""
        if self.path.endswith("/config"):
            cfg = load(CONFIG, {})
            for k in ("whatsapp_key", "mcp_token"):
                v = (form.get(k) or [""])[0].strip()
                if v:
                    cfg[k] = v
            save(CONFIG, cfg)
            st = load(STATE, {})
            st.pop("recipient", None)
            save(STATE, st)
            notice = "Saved. The next check runs within a minute."
        elif self.path.endswith("/test"):
            cfg, st = load(CONFIG, {}), load(STATE, {})
            to = recipient(cfg.get("whatsapp_key", ""), st) if cfg.get("whatsapp_key") else None
            if not to:
                notice = "No recipient yet: add the key and send your WhatsApp agent any message first."
            else:
                ok, code = send(cfg["whatsapp_key"], to, rules.format_alerts([rules.alert("info", "test", "Umbrel Alerts is on", "You'll hear from me here when something needs attention.")]))
                save(STATE, st)
                notice = "Test alert sent." if ok else f"WhatsApp refused the message ({code})."
        self.page(notice)


if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
