import gc
import os
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tools.inventory_scanner.workflow import Config, ScannerConfig, Service, Target
from tools.inventory_scanner.zebra import Event, ScannerError
from tools.inventory_scanner.tests.test_workflow import FakeClient, bottle


@unittest.skipUnless(os.environ.get("DISPLAY"), "Multi-scanner desktop tests require a display")
class MultiScannerDesktopTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tools.inventory_scanner.gui import Station
        self.client = FakeClient()
        for number in (1, 2):
            item = bottle(number)
            self.client.records[item.ref.key] = item
            self.client.refs[f"code-{number}"] = item.ref
        config = Config(
            "https://eln.example", fullscreen=False, scanner_backend="zebra_ssi",
            scanners=(ScannerConfig("blue", "Blue scanner", "/dev/blue"),
                      ScannerConfig("orange", "Orange scanner", "/dev/orange")),
        )
        self.scanners = {}

        def scanner_factory(port, *_):
            scanner = Mock()
            scanner.failure = None
            scanner.events = queue.Queue()
            scanner.start.side_effect = lambda: scanner.events.put(Event("ready", "Connected"))
            self.scanners[port] = scanner
            return scanner

        self.root = tk.Tk()
        with patch("tools.inventory_scanner.gui.ZebraScanner", side_effect=scanner_factory):
            self.station = Station(self.root, Service(self.client, config))
        self.root.geometry("800x480")
        self.pump(lambda: all(panel.ready and panel.controller.state == "idle"
                             for panel in self.station.hub.panels.values()))

    def tearDown(self):
        self.pump(lambda: all(panel.controller.state not in {
            "startup", "loading", "saving", "audit_starting", "audit_saving", "finishing",
        } for panel in self.station.hub.panels.values()))
        for panel in self.station.hub.panels.values():
            panel.controller.close()
        self.root.destroy()
        del panel
        del self.station, self.root
        gc.collect()

    def pump(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(0.01)
        self.fail([(key, panel.controller.state, panel.controller.message)
                   for key, panel in self.station.hub.panels.items()])

    def model(self, identifier):
        return self.station.hub.panels[identifier].controller

    def scan(self, identifier, code):
        self.scanners[f"/dev/{identifier}"].events.put(Event("scan", code))
        self.station.poll_scanner()

    def test_panel_switches_on_scans_not_background_results_and_routes_feedback(self):
        self.scan("blue", "code-1")
        self.scan("orange", "code-2")
        self.pump(lambda: self.model("blue").state == self.model("orange").state == "destination")
        self.assertEqual(self.station.hub.active_id, "orange")
        self.assertIn("sample:2", self.station.details.get("1.0", "end"))
        self.scan("blue", Target("location", "Group A", "Blue Building", collection_id=10).payload())
        self.scan("orange", Target("location", "Group B", "Orange Building", collection_id=20).payload())
        self.pump(lambda: self.model("blue").state == self.model("orange").state == "idle")
        self.assertEqual(self.station.hub.active_id, "orange")
        self.assertEqual(self.client.records["sample:1"].first["current_building"], "Blue Building")
        self.assertEqual(self.client.records["sample:2"].first["current_building"], "Orange Building")
        self.assertEqual(self.client.records["sample:2"].first["current_group"], "Group B")
        self.assertIn("Orange scanner", self.station.scanner_selector.get())
        for scanner in self.scanners.values():
            self.assertEqual([call.args[0] for call in scanner.feedback.call_args_list],
                             ["recognized", "success"])

    def test_simultaneous_queued_events_follow_actual_timestamp_not_device_poll_order(self):
        self.scanners["/dev/blue"].events.put(Event("scan", "code-1", timestamp=20))
        self.scanners["/dev/orange"].events.put(Event("scan", "code-2", timestamp=10))
        self.station.poll_scanner()
        self.assertEqual(self.station.hub.active_id, "blue")
        self.pump(lambda: self.model("blue").state == self.model("orange").state == "destination")
        self.assertEqual(self.station.hub.active_id, "blue")

    def test_hidden_confirmation_does_not_open_modal_or_change_selected_scanner(self):
        self.client.records["sample:1"].first["status"] = "Missing"
        self.scan("blue", "code-1")
        self.pump(lambda: self.model("blue").state == "destination")
        target = Target("location", "Group A", "B2", collection_id=10).payload()
        with patch("tools.inventory_scanner.gui.messagebox.askyesno") as ask:
            self.scan("blue", target)
            self.scan("orange", "code-2")
            self.pump(lambda: self.model("blue").awaiting_found is not None
                      and self.model("orange").state == "destination")
            ask.assert_not_called()
        self.assertEqual(self.station.hub.active_id, "orange")
        self.station.scanner_selector.current(0)
        self.station.select_scanner()
        self.assertTrue(self.station.found_button.winfo_manager())
        self.root.update_idletasks()
        self.assertLessEqual(self.station.found_button.winfo_rootx()
                             + self.station.found_button.winfo_width(),
                             self.root.winfo_rootx() + self.root.winfo_width())
        self.assertLessEqual(self.station.found_button.winfo_rooty()
                             + self.station.found_button.winfo_height(),
                             self.root.winfo_rooty() + self.root.winfo_height())
        with patch("tools.inventory_scanner.gui.messagebox.askyesno", return_value=True):
            self.station.confirm_selected()
        self.pump(lambda: self.model("blue").state == "idle")
        self.assertEqual(self.client.records["sample:1"].first["status"], "Available")
        self.assertEqual(self.model("orange").pending.inventory.ref.id, 2)

    def test_inactive_disconnect_leaves_other_scanner_usable(self):
        self.scan("orange", "code-2")
        self.pump(lambda: self.model("orange").state == "destination")
        self.scanners["/dev/blue"].failure = "Disconnected"
        self.station.poll_scanner()
        self.assertEqual(self.station.hub.active_id, "orange")
        self.assertTrue(self.station.scanner_ready)
        self.assertFalse(self.station.hub.panels["blue"].ready)
        self.scan("orange", Target("location", "Group A", "B2", collection_id=10).payload())
        self.pump(lambda: self.model("orange").state == "idle")
        self.scanners["/dev/orange"].feedback.assert_called_with("success")

    def test_failed_scanner_callback_does_not_discard_other_scanners_events(self):
        original = self.station.scanner_event

        def fail_blue(scanner_id, event):
            if scanner_id == "blue":
                raise RuntimeError("blue callback failed")
            return original(scanner_id, event)

        self.scanners["/dev/blue"].events.put(Event("scan", "code-1", timestamp=10))
        self.scanners["/dev/orange"].events.put(Event("scan", "code-2", timestamp=20))
        with patch.object(self.station, "scanner_event", side_effect=fail_blue):
            with self.assertLogs("tools.inventory_scanner.controller", level="ERROR"):
                self.station.poll_scanner()
        self.pump(lambda: self.model("orange").state == "destination")
        self.assertIn("blue callback failed", self.model("blue").message)
        self.assertEqual(self.station.hub.active_id, "orange")
        self.scan("blue", "code-1")
        self.pump(lambda: self.model("blue").state == "destination")

    def test_active_controls_do_not_reset_another_scanners_pair(self):
        self.scan("blue", "code-1")
        self.scan("orange", "code-2")
        self.pump(lambda: self.model("blue").state == self.model("orange").state == "destination")
        self.station.reset()
        self.assertEqual(self.model("orange").state, "idle")
        self.assertEqual(self.model("blue").state, "destination")
        self.assertEqual(self.model("blue").pending.inventory.ref.id, 1)
        self.assertLessEqual(self.station.scanner_selector.winfo_rootx()
                             + self.station.scanner_selector.winfo_width(),
                             self.root.winfo_rootx() + self.root.winfo_width())

    def test_exit_checks_hidden_inflight_operations(self):
        self.scan("blue", "code-1")
        self.station.hub.select("orange")
        with patch("tools.inventory_scanner.gui.messagebox.showwarning") as warning:
            self.station.close()
            warning.assert_called_once()
        self.assertTrue(self.root.winfo_exists())

    def test_hidden_audit_changes_refresh_header_without_changing_selection(self):
        started, release = threading.Event(), threading.Event()
        original = self.client.inventories

        def delayed(ids):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test baseline timeout")
            return original(ids)

        self.client.inventories = delayed
        self.model("blue").set_mode("Audit")
        try:
            self.scan("blue", Target(building="B1", room="204", cabinet="C3",
                                     group="Group A", collection_id=10).payload())
            self.assertTrue(started.wait(2))
            self.station.hub.select("orange")
            self.station.render()
            self.assertNotIn("Audit on blue", self.station.heading.cget("text"))
        finally:
            release.set()
        self.pump(lambda: self.model("blue").state == "audit_ready")
        self.assertEqual(self.station.hub.active_id, "orange")
        self.assertIn("Audit on blue", self.station.heading.cget("text"))
        self.model("blue").reset()
        self.assertNotIn("Audit on blue", self.station.heading.cget("text"))

    def test_cleanup_attempts_other_devices_and_worker_after_close_error(self):
        self.scanners["/dev/blue"].close.side_effect = ScannerError("close failed")
        with patch.object(self.station.hub.executor, "shutdown",
                          wraps=self.station.hub.executor.shutdown) as shutdown:
            with self.assertLogs("tools.inventory_scanner.gui", level="ERROR"):
                self.station.cleanup()
            self.scanners["/dev/orange"].close.assert_called_once()
            shutdown.assert_called_once()
        self.station.cleanup()


if __name__ == "__main__":
    unittest.main()
