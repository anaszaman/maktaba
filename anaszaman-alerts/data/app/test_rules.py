import os
import subprocess
import sys
import unittest

import rules


def snap(**kw):
    base = {"apps": {"jellyfin": "ready"}, "names": {"jellyfin": "Jellyfin", "kiwix": "Kiwix"}, "updates": [],
            "uptime": 1000, "disks": {"Internal SSD": (20, 100)}, "mounts": ["ssd"], "temp": 60,
            "temp_warning": "normal", "memory_fraction": 0.3, "notifications": []}
    base.update(kw)
    return base


class Rules(unittest.TestCase):
    def run_pair(self, a, b, t=1000.0, dt=60):
        _, s = rules.evaluate({}, a, t)
        return rules.evaluate(s, b, t + dt)

    def test_first_check_is_silent(self):
        alerts, s = rules.evaluate({}, snap(apps={"jellyfin": "unknown"}, disks={"Internal SSD": (97, 100)}), 0)
        self.assertEqual(alerts, [])
        self.assertTrue(s["initialised"])

    def test_app_problem_then_recovery(self):
        alerts, s = self.run_pair(snap(), snap(apps={"jellyfin": "unknown"}))
        self.assertEqual([a["key"] for a in alerts], ["app:jellyfin"])
        again, s = rules.evaluate(s, snap(apps={"jellyfin": "unknown"}), 2000)
        self.assertEqual(again, [])
        back, _ = rules.evaluate(s, snap(), 2100)
        self.assertEqual([a["severity"] for a in back], ["resolved"])

    def test_stopping_an_app_is_quiet_but_stuck_updating_is_not(self):
        alerts, s = self.run_pair(snap(), snap(apps={"jellyfin": "stopped"}))
        self.assertEqual(alerts, [])
        _, s = rules.evaluate(s, snap(apps={"jellyfin": "updating"}), 2000)
        soon, s = rules.evaluate(s, snap(apps={"jellyfin": "updating"}), 2000 + 600)
        self.assertEqual(soon, [])
        late, _ = rules.evaluate(s, snap(apps={"jellyfin": "updating"}), 2000 + 16 * 60)
        self.assertIn("updating", late[0]["body"])

    def test_storage_levels_with_hysteresis(self):
        alerts, s = self.run_pair(snap(), snap(disks={"Internal SSD": (91, 100)}))
        self.assertEqual(alerts[0]["severity"], "warning")
        hold, s = rules.evaluate(s, snap(disks={"Internal SSD": (88, 100)}), 3000)
        self.assertEqual(hold, [])
        crit, s = rules.evaluate(s, snap(disks={"Internal SSD": (96, 100)}), 3100)
        self.assertEqual(crit[0]["severity"], "critical")
        ok, _ = rules.evaluate(s, snap(disks={"Internal SSD": (80, 100)}), 3200)
        self.assertEqual(ok[0]["severity"], "resolved")

    def test_drive_disconnect_and_reconnect(self):
        gone, s = self.run_pair(snap(), snap(mounts=[]))
        self.assertIn("disconnected", gone[0]["title"])
        back, _ = rules.evaluate(s, snap(), 5000)
        self.assertIn("connected", back[0]["title"])

    def test_restart_detected_from_uptime(self):
        alerts, _ = self.run_pair(snap(uptime=50000), snap(uptime=30), t=1000, dt=900)
        self.assertTrue(alerts[0]["key"].startswith("restart:"))
        self.assertIn("offline", alerts[0]["body"])

    def test_only_new_updates_and_notifications(self):
        _, s = rules.evaluate({}, snap(updates=["jellyfin"], notifications=["old"]), 0)
        alerts, s = rules.evaluate(s, snap(updates=["jellyfin", "kiwix"], notifications=["old", "Backup failed"]), 60)
        self.assertTrue(alerts[0]["key"].startswith("updates:"))
        self.assertEqual(alerts[1]["key"], "note:" + rules.stable_key("Backup failed"))
        self.assertEqual(alerts[0]["body"], "Kiwix")
        quiet, _ = rules.evaluate(s, snap(updates=["jellyfin", "kiwix"], notifications=["old", "Backup failed"]), 120)
        self.assertEqual(quiet, [])

    def test_temperature_and_memory(self):
        alerts, s = self.run_pair(snap(), snap(temp_warning="warm", temp=82, memory_fraction=0.97))
        self.assertEqual({a["key"] for a in alerts}, {"temp", "memory"})
        ok, _ = rules.evaluate(s, snap(), 9000)
        self.assertEqual({a["severity"] for a in ok}, {"resolved"})

    def test_note_key_is_deterministic_for_the_same_text(self):
        self.assertEqual(rules.stable_key("Backup failed"), rules.stable_key("Backup failed"))
        self.assertNotEqual(rules.stable_key("Backup failed"), rules.stable_key("Backup failed!"))
        self.assertRegex(rules.stable_key("x"), r"^[0-9a-f]{12}$")

    def test_note_key_is_stable_across_processes(self):
        # hash() would differ between these two runs; the alert key must not.
        code = ("import rules; _, s = rules.evaluate({}, %r, 0); "
                "a, _ = rules.evaluate(s, dict(%r, notifications=['Backup failed']), 60); print(a[0]['key'])")
        keys = set()
        for seed in ("1", "2"):
            out = subprocess.run([sys.executable, "-c", code % (snap(), snap())], capture_output=True, text=True,
                                 cwd=os.path.dirname(os.path.abspath(__file__)), check=True,
                                 env=dict(os.environ, PYTHONHASHSEED=seed))
            keys.add(out.stdout.strip())
        self.assertEqual(keys, {"note:" + rules.stable_key("Backup failed")})

    def test_repeat_events_get_distinct_keys(self):
        _, s = rules.evaluate({}, snap(uptime=500), 0)
        first, s = rules.evaluate(s, snap(uptime=10), 100)
        _, s = rules.evaluate(s, snap(uptime=900), 1000)
        second, _ = rules.evaluate(s, snap(uptime=10), 2000)
        self.assertNotEqual(first[0]["key"], second[0]["key"])

if __name__ == "__main__":
    unittest.main()
