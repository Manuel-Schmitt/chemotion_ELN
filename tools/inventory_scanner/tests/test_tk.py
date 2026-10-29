import gc
import os
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tools.inventory_scanner.api import PermissionDeniedError
from tools.inventory_scanner.workflow import Config, Service, Target
from tools.inventory_scanner.zebra import Event, ScannerError
from tools.inventory_scanner.tests.test_workflow import FakeClient, bottle


@unittest.skipUnless(os.environ.get("DISPLAY"), "Tk smoke tests need a display (for example xvfb-run)")
class DesktopSmokeTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tools.inventory_scanner.gui import Station
        self.client = FakeClient()
        item = bottle()
        self.client.records[item.ref.key] = item
        self.code = "0000000012345678901234567890123456789012"
        self.client.refs[self.code] = item.ref
        original_auth = self.client.authenticate

        def authenticate_off_ui_thread():
            self.assertNotEqual(threading.current_thread(), threading.main_thread())
            return original_auth()

        self.client.authenticate = authenticate_off_ui_thread
        config = Config("https://eln.example", fullscreen=False)
        self.root = tk.Tk()
        self.root.geometry("800x480")
        self.station = Station(self.root, Service(self.client, config))
        self.root.geometry("800x480")
        self.root.focus_force()
        self.wait("idle")

    def tearDown(self):
        self.station.controller.close()
        self.root.destroy()
        del self.station, self.root
        # Release Tk cycles on the UI thread before later tests start serial workers.
        gc.collect()

    def wait(self, state):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.root.update()
            if self.station.controller.state == state:
                self.root.update_idletasks()
                return
            time.sleep(0.01)
        self.fail(f"Expected {state}, got {self.station.controller.state}: {self.station.controller.message}")

    def enter(self, code):
        self.station.code.focus_force()
        self.station.code.insert(0, code)
        self.station.code.event_generate("<Return>")
        self.root.update()

    def test_keyboard_pair_and_controls_fit_small_display(self):
        self.enter(self.code)
        self.wait("destination")
        self.enter(Target("location", "Group A", "B2", collection_id=10).payload())
        self.wait("idle")
        self.assertEqual(self.client.records["sample:1"].first["current_building"], "B2")
        self.assertEqual(len(self.client.writes), 1)
        for widget in (self.station.code, self.station.details, self.station.finish,
                       self.station.reset_button, self.station.reconnect):
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())
        self.assertIn("Saved and verified", self.station.status.cget("text"))

    def test_long_destination_and_error_keep_controls_visible(self):
        self.station.change_mode("Audit")
        target = Target(building="B" * 100, room="R" * 100, cabinet="C" * 100,
                        group="G" * 100, borrower="A" * 100, collection_id=10)
        self.enter(target.payload())
        self.wait("audit_ready")
        self.station.controller.notify("Write denied: " + "permission details " * 100, sound="error")
        self.station.scanner_status = "Scanner failure: " + "connection details " * 100
        self.station.render()
        for geometry in ("800x480", "720x440"):
            self.root.geometry(geometry)
            self.root.update_idletasks()
            for widget in (self.station.code, self.station.finish, self.station.reset_button,
                           self.station.reconnect):
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                     self.root.winfo_rooty() + self.root.winfo_height())
                self.assertGreater(widget.winfo_height(), 1)
        details = self.station.details.get("1.0", "end")
        self.assertIn(target.scope_caption, details)
        self.assertIn("permission details " * 100, details)

    def test_found_recovery_actions_fit_without_clipped_buttons(self):
        self.client.records["sample:1"].first["status"] = "Missing"
        self.enter(self.code)
        self.wait("destination")
        self.enter(Target(building="B2").payload())
        self.wait("destination")
        for geometry in ("800x480", "720x440"):
            self.root.geometry(geometry)
            self.root.update_idletasks()
            for widget in (self.station.finish, self.station.found_button,
                           self.station.reset_button, self.station.reconnect):
                self.assertTrue(widget.winfo_ismapped())
                self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth())
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                     self.root.winfo_rootx() + self.root.winfo_width())
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                     self.root.winfo_rooty() + self.root.winfo_height())

    def test_polling_survives_unexpected_device_event_failure(self):
        self.root.after_cancel(self.station.poll_id)
        self.station.poll_id = None
        with patch.object(self.station, "poll_scanner", side_effect=RuntimeError("device callback failed")):
            with self.assertLogs("tools.inventory_scanner.controller", level="ERROR"):
                self.station.poll()
        self.assertIsNotNone(self.station.poll_id)
        self.assertIn("device callback failed", self.station.controller.message)
        self.enter(self.code)
        self.wait("destination")

    def test_tk_callback_error_preserves_inflight_write(self):
        self.enter(self.code)
        self.wait("destination")
        started, release = threading.Event(), threading.Event()
        original = self.client.update_inventory

        def delayed(latest, data):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test write timeout")
            return original(latest, data)

        self.client.update_inventory = delayed
        try:
            self.enter(Target(building="B2").payload())
            self.assertTrue(started.wait(2))
            exc = RuntimeError("button callback failed")
            with self.assertLogs("tools.inventory_scanner.controller", level="ERROR"):
                self.root.report_callback_exception(type(exc), exc, exc.__traceback__)
            self.assertEqual(self.station.controller.state, "saving")
            self.assertEqual(len(self.client.writes), 0)
        finally:
            release.set()
        self.wait("idle")
        self.assertEqual(len(self.client.writes), 1)

    def test_audit_finish_and_late_recovery_via_real_ui(self):
        self.station.change_mode("Audit")
        self.enter(Target("location", "Group A", "B1", "204", "C3", collection_id=10).payload())
        self.wait("audit_ready")
        self.assertIn("Collection 10", self.station.details.get("1.0", "end"))
        self.station.finish_audit()
        self.wait("idle")
        self.assertEqual(self.client.records["sample:1"].first["status"], "Missing")
        self.enter(Target("location", "Group A", "B1", "204", "C7", collection_id=10).payload())
        self.wait("audit_ready")
        self.enter(self.code)
        self.wait("audit_ready")
        self.assertEqual(self.client.records["sample:1"].first["status"], "Available")
        self.assertEqual(self.client.records["sample:1"].first["current_cabinet"], "C7")
        self.assertIn("C7", self.station.details.get("1.0", "end"))
        self.station.finish_audit()
        self.wait("idle")
        self.assertFalse(self.station.hub.audit.active)

    def test_fullscreen_command_and_escape_binding(self):
        self.assertIsNotNone(self.root.bind("<Escape>"))
        self.assertIsNotNone(self.root.bind("<F11>"))
        with patch.object(self.root, "attributes") as attributes:
            attributes.return_value = False
            self.station.fullscreen()
            attributes.assert_called_with("-fullscreen", True)

    def attach_scanner(self):
        scanner = Mock()
        scanner.events = queue.Queue()
        scanner.failure = None
        self.station.scanner = scanner
        self.station.scanner_ready = False
        scanner.events.put(Event("ready", "Test SSI connection verified"))
        self.station.poll_scanner()
        self.assertTrue(self.station.scanner_ready)
        return scanner

    def test_serial_events_reach_workflow_and_trigger_correct_sounds(self):
        scanner = self.attach_scanner()
        scanner.events.put(Event("scan", self.code))
        self.station.poll_scanner()
        self.wait("destination")
        scanner.feedback.assert_called_once_with("recognized")
        scanner.events.put(Event("scan", Target("location", "Group A", "B2", collection_id=10).payload()))
        self.station.poll_scanner()
        self.wait("idle")
        self.assertEqual([call.args[0] for call in scanner.feedback.call_args_list], ["recognized", "success"])
        self.assertEqual(self.client.records["sample:1"].first["current_building"], "B2")

    def test_scanner_sound_failure_preserves_verified_outcome_but_blocks_input(self):
        scanner = self.attach_scanner()
        self.enter(self.code)
        self.wait("destination")
        scanner.feedback.side_effect = ScannerError("Cable disconnected")
        self.enter(Target("location", "Group A", "B2", collection_id=10).payload())
        self.wait("idle")
        self.assertIn("Saved and verified", self.station.controller.message)
        self.assertFalse(self.station.scanner_ready)
        self.assertIn("Cable disconnected", self.station.scanner_status)
        self.assertEqual(str(self.station.code.cget("state")), "disabled")
        self.assertEqual(len(self.client.writes), 1)

    def test_forbidden_write_displays_permission_error_and_sounds_failure(self):
        scanner = self.attach_scanner()
        self.enter(self.code)
        self.wait("destination")
        self.client.update_inventory = Mock(side_effect=PermissionDeniedError(
            "Write denied: the API token lacks permission to update this inventory (HTTP 403). "
            "Grant its account Edit elements access to the inventory collection, then rescan."
        ))
        self.enter(Target("location", "Group B", "B2", collection_id=20).payload())
        self.wait("destination")
        self.assertIn("API token lacks permission", self.station.status.cget("text"))
        self.assertIn("Edit elements", self.station.details.get("1.0", "end"))
        scanner.feedback.assert_called_with("error")
        self.assertFalse(self.client.writes)
        self.assertEqual(self.root.winfo_height(), 480)
        self.assertLessEqual(self.station.reset_button.winfo_rooty() + self.station.reset_button.winfo_height(),
                             self.root.winfo_rooty() + self.root.winfo_height())

    def test_transport_failure_poisoned_audit_requires_cancellation(self):
        scanner = self.attach_scanner()
        self.station.change_mode("Audit")
        self.enter(Target("location", "Group A", "B1", "204", "C3", collection_id=10).payload())
        self.wait("audit_ready")
        scanner.failure = "Read failed"
        self.station.poll_scanner()
        self.assertFalse(self.station.scanner_ready)
        self.assertEqual(str(self.station.finish.cget("state")), "disabled")
        scanner.failure = None
        scanner.events.put(Event("ready", "Reconnected"))
        self.station.poll_scanner()
        self.station.finish_audit()
        self.wait("audit_ready")
        self.assertIn("failed to load", self.station.controller.message)
        self.assertFalse(self.client.writes)

    def test_external_audit_scan_keeps_scope_visible_and_routes_feedback(self):
        scanner = self.attach_scanner()
        self.station.change_mode("Audit")
        self.enter(Target("location", "Group A", "B1", "204", "C3", collection_id=10).payload())
        self.wait("audit_ready")
        outsider = bottle(2, "sequence_based_macromolecule_sample", "Missing", cabinet="Elsewhere")
        self.client.records[outsider.ref.key] = outsider
        self.client.refs["outside"] = outsider.ref
        scanner.events.put(Event("scan", "outside"))
        self.station.poll_scanner()
        self.wait("audit_ready")
        self.assertIn("Collection 10", self.station.details.get("1.0", "end"))
        self.assertIn("memberships unchanged", self.station.details.get("1.0", "end"))
        self.assertEqual(self.station.controller.audit.summary()["outside_collection"], 1)
        scanner.feedback.assert_called_with("success")
        self.assertEqual(self.client.records[outsider.ref.key].first["status"], "Available")
        self.assertEqual(self.root.winfo_height(), 480)
        for widget in (self.station.code, self.station.finish, self.station.scanner_selector):
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())
        self.station.finish_audit()
        self.wait("idle")
        self.assertEqual(self.client.records["sample:1"].first["status"], "Missing")
        self.assertEqual(self.client.records[outsider.ref.key].first["status"], "Available")


if __name__ == "__main__":
    unittest.main()
