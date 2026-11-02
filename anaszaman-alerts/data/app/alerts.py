"""Umbrel Alerts: watches this Umbrel through its MCP endpoint and sends alerts to a push hub.

Runs as an umbrelOS app. Config and state live in /state (the app's data folder).

Push hub API (Bearer push_secret):
  POST {push_url}/v1/alerts  {"alerts": [{key?, severity, title, body?, at?}, ...]}  (max 100)
       idempotent on (key, severity, at), so a retried alert always carries its original `at`.
  POST {push_url}/v1/status  {at, sections: [{title, items: [{label, value, state, detail?}]}]}
       the latest snapshot for the phone's status screen; every post is also a heartbeat.
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
INTERVAL = 60
CONFIG = os.path.join(STATE_DIR, "config.json")
STATE = os.path.join(STATE_DIR, "state.json")

BATCH = 100            # hub limit per request
OUTBOX_CAP = 1000      # oldest alerts are dropped past this
TITLE_MAX, BODY_MAX = 200, 4000

lock = threading.Lock()
status = {"last_check": None, "last_error": None, "last_push_ok": None, "outbox": 0}


def log(msg):
    print(msg, flush=True)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def stamp(at=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))


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


# ---------------- Push hub ----------------

def post_json(url, secret, body, timeout=15):
    """POST JSON with the Bearer secret. Returns the HTTP status, or 0 if the hub was unreachable."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Bearer " + secret.strip(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def ok(code):
    return 200 <= code < 300


def describe(code):
    if code == 0:
        return "Push hub unreachable"
    if code == 401:
        return "Push hub rejected the secret (401)"
    return f"Push hub answered {code}"


def outbox_item(a, at_ms):
    """A rules alert as it waits in the outbox: everything the hub needs, `at` fixed at creation."""
    return {"key": a.get("key"), "severity": a["severity"], "title": a["title"], "body": a.get("body", ""), "at": at_ms}


def alert_in(item):
    """Outbox item → the hub's AlertIn (trimmed to its limits so one long text can't fail a batch)."""
    out = {"severity": item["severity"], "title": (item.get("title") or "Umbrel")[:TITLE_MAX], "at": item["at"]}
    if item.get("key"):
        out["key"] = item["key"]
    if item.get("body"):
        out["body"] = item["body"][:BODY_MAX]
    return out


def clean_outbox(outbox):
    """Drop anything that isn't a structured alert (older versions queued pre-formatted text)."""
    items = [i for i in outbox or [] if isinstance(i, dict) and i.get("severity") and i.get("at")]
    if len(items) != len(outbox or []):
        log(f"outbox: discarded {len(outbox) - len(items)} old-format item(s)")
    return items


def enqueue(outbox, items, cap=OUTBOX_CAP):
    outbox = outbox + items
    if len(outbox) > cap:
        log(f"outbox: over {cap}, dropped the {len(outbox) - cap} oldest alert(s)")
        outbox = outbox[-cap:]
    return outbox


def deliver(outbox, url, secret, post=post_json):
    """Send up to BATCH queued alerts in one request. → (remaining outbox, error or None).

    2xx clears the batch. 400 means the batch as a whole was refused, so its items go one by one
    and only those the hub refuses individually are dropped. Anything else (unreachable, 401,
    429, 5xx) keeps every item for the next minute; nothing is dropped for being retried a lot.
    """
    if not outbox:
        return outbox, None
    endpoint = url.rstrip("/") + "/v1/alerts"
    batch, rest = outbox[:BATCH], outbox[BATCH:]
    code = post(endpoint, secret, {"alerts": [alert_in(i) for i in batch]})
    if ok(code):
        return rest, None
    if code != 400:
        return outbox, describe(code) + "; will retry next minute."
    keep, error = [], None
    for n, item in enumerate(batch):
        code = post(endpoint, secret, {"alerts": [alert_in(item)]})
        if ok(code):
            continue
        if code == 400:
            log(f"outbox: hub refused alert {json.dumps(alert_in(item))[:500]}; dropped")
            continue
        keep, error = batch[n:], describe(code) + "; will retry next minute."
        break
    return keep + rest, error


# ---------------- Status snapshot ----------------

RUNNING = {"ready", "running"}


def pct_state(frac):
    return "crit" if frac >= rules.STORAGE_CRIT else "warn" if frac >= rules.STORAGE_WARN else "ok"


def gb(n):
    return f"{n / 1e9:.1f}"


def build_status(now, at):
    """Snapshot dict → the hub's status body."""
    names, states = now.get("names", {}), now.get("apps", {})
    running = sum(1 for s in states.values() if s in RUNNING)
    broken = [a for a, s in states.items() if s not in rules.NORMAL and s not in rules.TRANSITIONAL]
    busy = [a for a, s in states.items() if s in rules.TRANSITIONAL]
    stopped = [a for a, s in states.items() if s == "stopped"]
    apps = [{"label": "Running", "value": f"{running} of {len(states)}",
             "state": "crit" if broken else "warn" if busy else "ok"}]
    for group, state in ((broken, "crit"), (busy, "warn"), (stopped, "ok")):
        for a in sorted(group, key=lambda a: names.get(a, a).lower()):
            apps.append({"label": names.get(a, a), "value": states[a] or "unknown", "state": state})

    storage = []
    for disk, (used, size) in now.get("disks", {}).items():
        frac = used / size if size else 0
        storage.append({"label": disk, "value": f"{frac * 100:.0f}%" if size else "unknown",
                        "state": pct_state(frac) if size else "unknown",
                        "detail": f"{gb(used)} of {gb(size)} GB used, {gb(max(0, size - used))} GB free"})

    temp, warning = now.get("temp"), (now.get("temp_warning") or "normal").lower()
    mem = now.get("memory_fraction") or 0
    updates = sorted(names.get(a, a) for a in now.get("updates", []))
    up = now.get("uptime")
    system = [
        {"label": "Temperature", "value": f"{temp:.0f} °C" if isinstance(temp, (int, float)) else "unknown",
         "state": "ok" if warning == "normal" else "crit" if warning in ("hot", "critical", "danger") else "warn",
         "detail": warning},
        {"label": "Memory", "value": f"{mem * 100:.0f}%", "state": "warn" if mem >= rules.MEMORY_WARN else "ok"},
        {"label": "Uptime", "value": rules.fmt_duration(up) if isinstance(up, (int, float)) else "unknown",
         "state": "ok" if isinstance(up, (int, float)) else "unknown"},
        {"label": "Updates", "value": str(len(updates)), "state": "ok",
         **({"detail": ", ".join(updates)} if updates else {})},
    ]
    return {"at": int(at * 1000), "sections": [
        {"title": "Apps", "items": apps},
        {"title": "Storage", "items": storage},
        {"title": "System", "items": system},
        {"title": "Last check", "items": [{"label": "Checked", "value": stamp(at), "state": "ok"}]},
    ]}


def failure_status(error, at):
    """Status body when the Umbrel couldn't be read, so a broken MCP token shows on the phone."""
    return {"at": int(at * 1000), "sections": [
        {"title": "Umbrel MCP", "items": [{"label": "MCP", "value": "Check failed", "state": "crit",
                                           "detail": str(error)[:1000]}]},
        {"title": "Last check", "items": [{"label": "Checked", "value": stamp(at), "state": "crit"}]},
    ]}


# ---------------- Loop ----------------

def check_once(post=post_json, read=None):
    cfg, state = load(CONFIG, {}), load(STATE, {})
    token = cfg.get("mcp_token", "")
    url, secret = cfg.get("push_url", ""), cfg.get("push_secret", "")
    outbox = clean_outbox(state.get("outbox", []))
    state.pop("recipient", None)  # from older versions
    errors, at = [], time.time()
    if not token:
        errors.append("Add the Umbrel MCP token on this page.")
        body = failure_status("No Umbrel MCP token saved on the app page.", at)
    else:
        try:
            now = (read or snapshot)(token)
            alerts, nxt = rules.evaluate(state.get("rules", {}), now, at)
            outbox = enqueue(outbox, [outbox_item(a, int(at * 1000)) for a in alerts])
            state["rules"] = nxt
            body = build_status(now, at)
        except Exception as e:
            traceback.print_exc()
            errors.append(f"Umbrel check failed: {type(e).__name__}: {e}")
            body = failure_status(f"{type(e).__name__}: {e}", at)
    state["outbox"] = outbox
    save(STATE, state)  # new alerts and rule state are on disk before any send

    if not url or not secret:
        errors.append("Add the push hub URL and secret on this page.")
    else:
        code = post(url.rstrip("/") + "/v1/status", secret, body)
        if ok(code):
            status["last_push_ok"] = stamp()
        else:
            errors.append(describe(code) + " to the status update.")
        before = len(outbox)
        outbox, err = deliver(outbox, url, secret, post)
        if err:
            errors.append(err)
        elif before:
            status["last_push_ok"] = stamp()
        if len(outbox) != before:
            state["outbox"] = outbox
            save(STATE, state)
    status.update(last_check=stamp(at), outbox=len(outbox), last_error=errors[0] if errors else None)


def loop():
    while True:
        try:
            with lock:
                check_once()
        except Exception as e:
            status["last_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        time.sleep(INTERVAL)


def send_test(post=post_json):
    cfg = load(CONFIG, {})
    url, secret = cfg.get("push_url", ""), cfg.get("push_secret", "")
    if not url or not secret:
        return "Add the push hub URL and secret first."
    item = {"severity": "info", "title": "Umbrel Alerts test", "at": int(time.time() * 1000),
            "body": "If this reached your phone, alerts from this Umbrel are working."}
    code = post(url.rstrip("/") + "/v1/alerts", secret, {"alerts": [item]})
    if ok(code):
        status["last_push_ok"] = stamp()
        return "Test alert sent."
    return describe(code) + "."


# ---------------- Setup page ----------------

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Umbrel Alerts</title><style>
:root{{color-scheme:light dark;--bg:#fff;--ink:#1d1d1f;--muted:#6e6e73;--panel:#f5f5f7;--line:#d2d2d7;--accent:#2d4fa3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--ink:#f5f5f7;--muted:#98989d;--panel:#1c1c1e;--line:#38383a;--accent:#8aa4ff}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
main{{max-width:560px;margin:0 auto;padding:40px 16px}} h1{{font-size:28px;margin:0 0 4px}} p.sub{{color:var(--muted);margin:0 0 28px}}
.card{{background:var(--panel);border-radius:14px;padding:16px;margin-bottom:16px}} .row{{display:flex;justify-content:space-between;gap:12px;padding:6px 0}}
.row span:first-child{{color:var(--muted);flex:none}} .row span:last-child{{text-align:right;overflow-wrap:anywhere}}
label{{display:block;font-weight:600;margin:12px 0 6px}} small{{color:var(--muted)}}
input{{width:100%;box-sizing:border-box;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit}}
button{{margin-top:16px;padding:10px 18px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}}
button.ghost{{background:transparent;color:var(--accent);border:1px solid var(--line)}} .ok{{color:#34c759}} .bad{{color:#ff9f0a}}
</style></head><body><main>
<h1>Umbrel Alerts</h1><p class="sub">Sends a push alert to your phone when something on this Umbrel needs attention.</p>
<div class="card">
<div class="row"><span>Status</span><span class="{cls}">{state}</span></div>
<div class="row"><span>Last check</span><span>{last_check}</span></div>
<div class="row"><span>Last error</span><span>{last_error}</span></div>
<div class="row"><span>Last push OK</span><span>{last_push_ok}</span></div>
<div class="row"><span>Waiting to send</span><span>{outbox}</span></div>
</div>
{notice}
<form class="card" method="post" action="config">
<label>Push hub URL</label><input name="push_url" type="text" value="{push_url}" placeholder="https://…" autocomplete="off" spellcheck="false">
<small>The base address of your push hub; alerts go to /v1/alerts and status to /v1/status.</small>
<label>Push secret</label><input name="push_secret" type="password" placeholder="{sec_ph}" autocomplete="off">
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
        saved = "Saved (leave empty to keep)"
        body = PAGE.format(
            cls="ok" if healthy else "bad",
            state="Watching" if healthy else ("Needs attention" if status["last_check"] else "Starting…"),
            last_check=html.escape(status["last_check"] or "–"),
            last_error=html.escape(status["last_error"] or "–"),
            last_push_ok=html.escape(status["last_push_ok"] or "–"),
            outbox=status["outbox"],
            notice=f'<div class="card">{html.escape(notice)}</div>' if notice else "",
            push_url=html.escape(cfg.get("push_url", ""), quote=True),
            sec_ph=saved if cfg.get("push_secret") else "From your push hub",
            tok_ph=saved if cfg.get("mcp_token") else "umbrelmcp_…",
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
            with lock:
                cfg = load(CONFIG, {})
                url = (form.get("push_url") or [""])[0].strip().rstrip("/")
                if url and not url.startswith(("https://", "http://")):
                    notice = "The push hub URL must start with https://. Nothing was saved."
                else:
                    if url:
                        cfg["push_url"] = url
                    for k in ("push_secret", "mcp_token"):  # empty keeps the saved value
                        v = (form.get(k) or [""])[0].strip()
                        if v:
                            cfg[k] = v
                    # keep only current settings, which drops keys left by older versions
                    save(CONFIG, {k: cfg[k] for k in ("push_url", "push_secret", "mcp_token") if k in cfg})
                    notice = "Saved. The next check runs within a minute."
        elif self.path.endswith("/test"):
            notice = send_test()
        self.page(notice)


if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
