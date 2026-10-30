"""Pure alert rules: compare what the Umbrel looks like now with the last check.

No I/O here so it unit-tests on any machine. Each rule returns alerts plus the state to keep.
"""

NORMAL = {"ready", "running", "stopped"}
TRANSITIONAL = {"starting", "stopping", "restarting", "installing", "updating", "uninstalling"}
STUCK_AFTER = 15 * 60          # seconds an app may sit in a transitional state
STORAGE_WARN, STORAGE_CRIT, STORAGE_CLEAR = 0.90, 0.95, 0.85
MEMORY_WARN, MEMORY_CLEAR = 0.95, 0.85


def alert(severity, key, title, body=""):
    return {"severity": severity, "key": key, "title": title, "body": body}


def evaluate(prev, now, at):
    """prev: last state dict (may be empty). now: snapshot dict. at: unix time. → (alerts, next_state)."""
    first = not prev.get("initialised")
    out = []
    nxt = {"initialised": True}

    # --- Restart detection: uptime went backwards ---
    up = now.get("uptime")
    last_up, last_seen = prev.get("uptime"), prev.get("seen_at")
    if not first and up is not None and last_up is not None and up < last_up:
        gone = f" after about {fmt_duration(at - last_seen - up)} offline" if last_seen and at - last_seen - up > 90 else ""
        out.append(alert("info", "restart", "Umbrel restarted", f"It's back online{gone}."))
    nxt["uptime"], nxt["seen_at"] = up, at

    # --- Apps ---
    names = now.get("names", {})
    states = now.get("apps", {})
    prev_states = prev.get("apps", {})
    since = dict(prev.get("since", {}))
    problems = set(prev.get("problems", []))
    for app, state in states.items():
        name = names.get(app, app)
        if prev_states.get(app) != state:
            since[app] = at
        bad = state not in NORMAL and state not in TRANSITIONAL
        stuck = state in TRANSITIONAL and at - since.get(app, at) >= STUCK_AFTER
        if (bad or stuck) and app not in problems:
            problems.add(app)
            if not first:
                why = f"It has been {state} for {fmt_duration(at - since[app])}." if stuck else f"Its state is “{state or 'unknown'}”."
                out.append(alert("warning", f"app:{app}", f"{name} needs attention", why))
        elif not (bad or stuck) and app in problems:
            problems.discard(app)
            out.append(alert("resolved", f"app:{app}", f"{name} is OK again", f"State: {state}."))
    problems &= set(states)
    nxt["apps"], nxt["since"], nxt["problems"] = states, {a: t for a, t in since.items() if a in states}, sorted(problems)

    # --- Updates: announce only newly available ones ---
    updates = set(now.get("updates", []))
    fresh = updates - set(prev.get("updates", []))
    if fresh and not first:
        listed = ", ".join(sorted(names.get(a, a) for a in fresh))
        out.append(alert("info", "updates", "App updates available", listed))
    nxt["updates"] = sorted(updates)

    # --- Storage: internal disk and every external drive ---
    levels = dict(prev.get("storage_level", {}))
    for disk, (used, size) in now.get("disks", {}).items():
        frac = used / size if size else 0
        level = "crit" if frac >= STORAGE_CRIT else "warn" if frac >= STORAGE_WARN else ("ok" if frac < STORAGE_CLEAR else levels.get(disk, "ok"))
        before = levels.get(disk, "ok")
        if level != before and not first:
            pct = f"{frac * 100:.0f}% used, {fmt_bytes(size - used)} free"
            if level == "crit":
                out.append(alert("critical", f"disk:{disk}", f"{disk} is almost full", pct))
            elif level == "warn" and before == "ok":
                out.append(alert("warning", f"disk:{disk}", f"{disk} is filling up", pct))
            elif level == "ok":
                out.append(alert("resolved", f"disk:{disk}", f"{disk} has space again", pct))
        levels[disk] = level
    nxt["storage_level"] = levels

    # --- External drives appearing / disappearing ---
    mounts, prev_mounts = set(now.get("mounts", [])), set(prev.get("mounts", []))
    if not first:
        for m in sorted(prev_mounts - mounts):
            out.append(alert("warning", f"mount:{m}", f"Drive “{m}” disconnected", "It's no longer mounted. Check the cable or power."))
        for m in sorted(mounts - prev_mounts):
            out.append(alert("resolved", f"mount:{m}", f"Drive “{m}” connected", ""))
    nxt["mounts"] = sorted(mounts)

    # --- Temperature ---
    warning = (now.get("temp_warning") or "normal").lower()
    hot, was_hot = warning != "normal", prev.get("hot", False)
    temp = now.get("temp")
    t = f"{temp:.0f} °C" if isinstance(temp, (int, float)) else warning
    if hot and not was_hot and not first:
        out.append(alert("warning", "temp", "Umbrel is running hot", f"Temperature {t} ({warning})."))
    elif was_hot and not hot:
        out.append(alert("resolved", "temp", "Temperature is normal again", t))
    nxt["hot"] = hot

    # --- Memory pressure ---
    mem = now.get("memory_fraction", 0)
    high = mem >= MEMORY_WARN or (prev.get("mem_high") and mem >= MEMORY_CLEAR)
    if high and not prev.get("mem_high") and not first:
        out.append(alert("warning", "memory", "Umbrel memory is nearly full", f"{mem * 100:.0f}% in use."))
    elif prev.get("mem_high") and not high:
        out.append(alert("resolved", "memory", "Memory use is back to normal", f"{mem * 100:.0f}% in use."))
    nxt["mem_high"] = bool(high)

    # --- Umbrel's own notifications ---
    notes, seen = now.get("notifications", []), set(prev.get("notifications", []))
    if not first:
        for n in notes:
            if n not in seen:
                out.append(alert("info", f"note:{hash(n)}", "Umbrel", n))
    nxt["notifications"] = notes[-200:]

    return out, nxt


MARK = {"critical": "🔴", "warning": "🟠", "resolved": "🟢", "info": "ℹ️"}


def format_alerts(alerts):
    """One alert as-is; a burst as one message with a count, like the Remantis outbox."""
    def one(a):
        return f"{MARK[a['severity']]} *{a['title']}*" + (f"\n{a['body']}" if a.get("body") else "")
    text = one(alerts[0]) if len(alerts) == 1 else "\n\n".join([f"🚨 *{len(alerts)} alerts*"] + [one(a) for a in alerts])
    return text if len(text) <= 4096 else text[:4080] + "\n…"


def fmt_bytes(n):
    n = max(0, n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000


def fmt_duration(s):
    s = int(max(0, s))
    if s >= 86400:
        return f"{s // 86400}d {s % 86400 // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{max(1, s // 60)} min"
