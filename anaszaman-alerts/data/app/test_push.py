"""Delivery to the push hub: outbox, retries, 400 splitting, status body. Stdlib only."""
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO

import alerts
import rules

URL, SECRET = "https://hub.test", "s3cret"


class FakeHub:
    """Injectable poster: records requests and answers with a scripted status code."""

    def __init__(self, answer=200):
        self.answer, self.calls = answer, []

    def __call__(self, url, secret, body):
        self.calls.append((url, secret, json.loads(json.dumps(body))))
        return self.answer(url, body) if callable(self.answer) else self.answer

    def alert_calls(self):
        return [b for u, _, b in self.calls if u.endswith("/v1/alerts")]


def items(n, start=0):
    return [alerts.outbox_item(rules.alert("warning", f"k{i}", f"Alert {i}", "body"), 1_000_000 + i)
            for i in range(start, start + n)]


def snap(**kw):
    base = {"apps": {"jellyfin": "ready", "sonarr": "stopped", "radarr": "updating", "kiwix": "unknown"},
            "names": {"jellyfin": "Jellyfin", "sonarr": "Sonarr", "radarr": "Radarr", "kiwix": "Kiwix"},
            "updates": ["jellyfin"], "uptime": 90000,
            "disks": {"Internal SSD": (50e9, 100e9), "Drive “ssd”": (96e9, 100e9)}, "mounts": ["ssd"],
            "temp": 61.4, "temp_warning": "normal", "memory_fraction": 0.42, "notifications": []}
    base.update(kw)
    return base


class Outbox(unittest.TestCase):
    def test_batch_success_clears_the_queue(self):
        hub = FakeHub(200)
        left, err = alerts.deliver(items(3), URL, SECRET, hub)
        self.assertEqual((left, err), ([], None))
        self.assertEqual(len(hub.calls), 1)
        url, secret, body = hub.calls[0]
        self.assertEqual((url, secret), (URL + "/v1/alerts", SECRET))
        self.assertEqual([a["key"] for a in body["alerts"]], ["k0", "k1", "k2"])
        self.assertEqual(set(body["alerts"][0]), {"key", "severity", "title", "body", "at"})

    def test_one_request_carries_at_most_100(self):
        hub = FakeHub(200)
        left, _ = alerts.deliver(items(250), URL, SECRET, hub)
        self.assertEqual(len(hub.calls), 1)
        self.assertEqual(len(hub.calls[0][2]["alerts"]), 100)
        self.assertEqual(left, items(150, start=100))

    def test_503_keeps_everything_and_retries_with_the_same_at(self):
        hub = FakeHub(503)
        box = items(2)
        left, err = alerts.deliver(box, URL, SECRET, hub)
        self.assertEqual(left, box)
        self.assertIn("503", err)
        hub.answer = 200
        alerts.deliver(left, URL, SECRET, hub)
        first, retry = hub.calls[0][2]["alerts"], hub.calls[1][2]["alerts"]
        self.assertEqual(first, retry)
        self.assertEqual([a["at"] for a in retry], [1_000_000, 1_000_001])

    def test_no_drop_after_many_failures(self):
        box = items(5)
        for code in [0, 401, 429, 500, 502, 503] * 50:
            box, err = alerts.deliver(box, URL, SECRET, FakeHub(code))
            self.assertTrue(err)
        self.assertEqual(box, items(5))
        self.assertIn("secret", alerts.deliver(box, URL, SECRET, FakeHub(401))[1])

    def test_poison_400_is_split_and_only_the_bad_item_dropped(self):
        def answer(url, body):
            batch = body["alerts"]
            return 400 if any(a["title"] == "Alert 1" for a in batch) else 200
        hub = FakeHub(answer)
        with redirect_stdout(StringIO()) as out:
            left, err = alerts.deliver(items(3), URL, SECRET, hub)
        self.assertEqual((left, err), ([], None))
        sent = [b["alerts"] for b in hub.alert_calls()]
        self.assertEqual(len(sent), 4)  # the batch, then one by one
        self.assertEqual([len(b) for b in sent[1:]], [1, 1, 1])
        self.assertIn("Alert 1", out.getvalue())

    def test_split_stops_and_keeps_the_rest_if_the_hub_goes_away(self):
        answers = iter([400, 200, 503])
        left, err = alerts.deliver(items(3), URL, SECRET, FakeHub(lambda u, b: next(answers)))
        self.assertEqual(left, items(2, start=1))
        self.assertIn("503", err)

    def test_cap_at_1000_drops_the_oldest(self):
        with redirect_stdout(StringIO()) as out:
            box = alerts.enqueue(items(990), items(20, start=990))
        self.assertEqual(len(box), 1000)
        self.assertEqual(box[0]["key"], "k10")
        self.assertEqual(box[-1]["key"], "k1009")
        self.assertIn("dropped the 10 oldest", out.getvalue())

    def test_old_string_items_are_discarded(self):
        with redirect_stdout(StringIO()) as out:
            box = alerts.clean_outbox(["🟠 *Old text*", items(1)[0], "another"])
        self.assertEqual(box, items(1))
        self.assertIn("discarded 2", out.getvalue())

    def test_alert_in_respects_hub_limits(self):
        a = alerts.alert_in({"key": None, "severity": "info", "title": "t" * 300, "body": "b" * 5000, "at": 5})
        self.assertEqual((len(a["title"]), len(a["body"])), (200, 4000))
        self.assertNotIn("key", a)


class Status(unittest.TestCase):
    def test_status_body_shape(self):
        body = alerts.build_status(snap(), 1_700_000_000)
        self.assertEqual(body["at"], 1_700_000_000_000)
        self.assertEqual([s["title"] for s in body["sections"]], ["Apps", "Storage", "System", "Last check"])
        for section in body["sections"]:
            for item in section["items"]:
                self.assertTrue({"label", "value", "state"} <= set(item))
                self.assertIn(item["state"], {"ok", "warn", "crit", "unknown"})
                self.assertIsInstance(item["value"], str)
        apps, storage, system, _ = (s["items"] for s in body["sections"])
        self.assertEqual(apps[0], {"label": "Running", "value": "1 of 4", "state": "crit"})
        self.assertEqual({i["label"]: i["state"] for i in apps[1:]}, {"Kiwix": "crit", "Radarr": "warn", "Sonarr": "ok"})
        self.assertEqual([(i["value"], i["state"]) for i in storage], [("50%", "ok"), ("96%", "crit")])
        self.assertIn("96.0 of 100.0 GB", storage[1]["detail"])
        sysmap = {i["label"]: i for i in system}
        self.assertEqual(sysmap["Temperature"]["value"], "61 °C")
        self.assertEqual(sysmap["Memory"]["value"], "42%")
        self.assertEqual((sysmap["Updates"]["value"], sysmap["Updates"]["detail"]), ("1", "Jellyfin"))
        self.assertEqual(sysmap["Uptime"]["value"], "1d 1h")
        self.assertLess(len(json.dumps(body).encode()), 64 * 1024)

    def test_storage_thresholds_match_the_rules(self):
        self.assertEqual([alerts.pct_state(f) for f in (0.89, 0.90, 0.949, 0.95)], ["ok", "warn", "warn", "crit"])


class CheckOnce(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.paths = alerts.CONFIG, alerts.STATE
        alerts.CONFIG = os.path.join(self.dir.name, "config.json")
        alerts.STATE = os.path.join(self.dir.name, "state.json")
        self.addCleanup(lambda: setattr(alerts, "CONFIG", self.paths[0]) or setattr(alerts, "STATE", self.paths[1]))
        alerts.save(alerts.CONFIG, {"mcp_token": "t", "push_url": URL + "/", "push_secret": SECRET})

    def run_check(self, hub, read):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            alerts.check_once(post=hub, read=read)
        return alerts.load(alerts.STATE, {})

    def test_mcp_failure_still_posts_a_crit_status(self):
        def broken(token):
            raise RuntimeError("401 Unauthorized")
        hub = FakeHub(200)
        self.run_check(hub, broken)
        url, _, body = hub.calls[0]
        self.assertEqual(url, URL + "/v1/status")
        item = body["sections"][0]["items"][0]
        self.assertEqual(item["state"], "crit")
        self.assertIn("401 Unauthorized", item["detail"])
        self.assertIn("Umbrel check failed", alerts.status["last_error"])

    def test_alerts_flow_from_rules_to_hub_and_old_text_is_dropped(self):
        alerts.save(alerts.STATE, {"outbox": ["old whatsapp-era text"], "recipient": "x"})
        hub = FakeHub(503)
        self.run_check(hub, lambda t: snap(disks={"Internal SSD": (10, 100)}))  # silent baseline
        state = self.run_check(hub, lambda t: snap(disks={"Internal SSD": (99, 100)}))
        self.assertNotIn("recipient", state)
        self.assertEqual([i["key"] for i in state["outbox"]], ["disk:Internal SSD"])
        self.assertEqual(alerts.status["outbox"], 1)
        hub.answer = 200
        state = self.run_check(hub, lambda t: snap(disks={"Internal SSD": (99, 100)}))
        self.assertEqual(state["outbox"], [])
        self.assertEqual(hub.alert_calls()[-1]["alerts"][0]["at"], hub.alert_calls()[0]["alerts"][0]["at"])
        self.assertIsNone(alerts.status["last_error"])
        self.assertEqual(sum(u.endswith("/v1/status") for u, _, _ in hub.calls), 3)

    def test_send_test_posts_one_keyless_info_alert(self):
        hub = FakeHub(200)
        self.assertEqual(alerts.send_test(post=hub), "Test alert sent.")
        (alert,) = hub.calls[0][2]["alerts"]
        self.assertEqual((alert["severity"], alert["title"]), ("info", "Umbrel Alerts test"))
        self.assertNotIn("key", alert)


class RealHttp(unittest.TestCase):
    def test_post_json_sends_bearer_and_returns_the_status(self):
        seen = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen["auth"] = self.headers.get("Authorization")
                seen["ua"] = self.headers.get("User-Agent")
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(503 if self.path == "/down" else 200)
                self.end_headers()
                self.wfile.write(b'{"accepted":1}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        self.assertEqual(alerts.post_json(base + "/v1/alerts", SECRET, {"alerts": []}), 200)
        # Not Python-urllib: Cloudflare answers that user agent with 403 (error 1010).
        self.assertEqual(seen, {"auth": "Bearer " + SECRET, "ua": "umbrel-alerts/1.1",
                                "body": {"alerts": []}})
        self.assertEqual(alerts.post_json(base + "/down", SECRET, {}), 503)
        server.shutdown()
        server.server_close()
        self.assertEqual(alerts.post_json(base + "/v1/alerts", SECRET, {}, timeout=2), 0)


if __name__ == "__main__":
    unittest.main()
