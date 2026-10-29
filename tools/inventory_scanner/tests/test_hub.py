import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from tools.inventory_scanner.hub import Hub
from tools.inventory_scanner.workflow import (
    Config, ScannerConfig, Service, Target, WorkflowError,
)
from tools.inventory_scanner.tests.test_workflow import FakeClient, bottle


class HubTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        for number in (1, 2):
            item = bottle(number, cabinet=f"C{number}")
            self.client.records[item.ref.key] = item
            self.client.refs[f"code-{number}"] = item.ref
        self.config = Config(
            "https://eln.example", scanner_backend="zebra_ssi",
            scanners=(ScannerConfig("blue", "Blue scanner", "/dev/blue"),
                      ScannerConfig("orange", "Orange scanner", "/dev/orange")),
        )
        self.changed, self.confirm, self.feedback = Mock(), Mock(), Mock()
        self.hub = Hub(Service(self.client, self.config), self.changed, self.confirm, self.feedback)
        self.addCleanup(self.hub.executor.shutdown, wait=True)
        for panel in self.hub.panels.values():
            panel.controller.connect()
        self.wait_all("idle")

    def wait_all(self, state):
        self.wait(lambda: all(panel.controller.state == state for panel in self.hub.panels.values()))

    def wait(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for panel in self.hub.panels.values():
                panel.controller.poll()
            if condition():
                return
            time.sleep(0.01)
        self.fail([(key, panel.controller.state, panel.controller.message) for key, panel in self.hub.panels.items()])

    def controller(self, identifier):
        return self.hub.panels[identifier].controller

    def test_independent_pairs_feedback_and_background_selection(self):
        blue, orange = self.controller("blue"), self.controller("orange")
        blue.scan("code-1")
        orange.scan("code-2")
        self.hub.select("orange")
        self.wait_all("destination")
        self.assertEqual(blue.pending.inventory.ref.id, 1)
        self.assertEqual(orange.pending.inventory.ref.id, 2)
        blue.scan(Target("location", "Group A", "Blue Building", collection_id=10).payload())
        orange.scan(Target("person", "Group A", borrower="Alice Current").payload())
        self.wait_all("idle")
        self.assertEqual(self.hub.active_id, "orange")
        self.assertEqual(self.client.records["sample:1"].first["current_building"], "Blue Building")
        self.assertEqual(self.client.records["sample:2"].first["borrowed_by"], "Alice Current")
        self.feedback.assert_any_call("blue", "success")
        self.feedback.assert_any_call("orange", "success")

    def test_closing_controller_does_not_shutdown_shared_worker(self):
        self.controller("blue").close()
        orange = self.controller("orange")
        orange.scan("code-2")
        self.wait(lambda: orange.state == "destination")
        self.assertEqual(orange.pending.inventory.ref.id, 2)

    def test_whole_operations_serialized_and_same_bottle_conflict(self):
        active = 0
        maximum = 0
        lock = threading.Lock()
        original = self.client.update_inventory

        def update(latest, data):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(active, maximum)
            time.sleep(0.03)
            result = original(latest, data)
            with lock:
                active -= 1
            return result

        self.client.update_inventory = update
        blue, orange = self.controller("blue"), self.controller("orange")
        blue.scan("code-1")
        orange.scan("code-1")
        self.wait_all("destination")
        blue.scan(Target("location", "Group A", "B2", collection_id=10).payload())
        orange.scan(Target("location", "Group A", "B3", collection_id=10).payload())
        self.wait(lambda: blue.state == "idle" and orange.state == "destination")
        self.assertEqual(maximum, 1)
        self.assertEqual(len(self.client.writes), 1)
        self.assertIn("changed", orange.message)
        self.assertEqual(orange.pending.inventory.first["current_building"], "B2")

    def test_reset_mode_and_timeout_are_local_to_one_scanner(self):
        blue, orange = self.controller("blue"), self.controller("orange")
        blue.scan("code-1")
        orange.scan("code-2")
        self.wait_all("destination")
        blue.reset()
        blue.set_mode("Lookup")
        self.assertEqual(orange.state, "destination")
        self.assertEqual(orange.pending.inventory.ref.id, 2)
        self.assertEqual(orange.mode, "Assign")
        blue.scan("code-1")
        self.wait(lambda: blue.state == "idle")
        self.assertFalse(self.client.writes)

    def test_only_one_terminal_audit_is_owned_by_the_scanner_that_started_it(self):
        blue, orange = self.controller("blue"), self.controller("orange")
        self.client.inventories = Mock(wraps=self.client.inventories)
        for model in (blue, orange):
            model.set_mode("Audit")
        blue.scan(Target("location", "Group A", "B1", "204", "C1", collection_id=10).payload())
        self.wait(lambda: blue.state == "audit_ready")
        orange.scan(Target("person", "Group B", "B1", "204", "C1",
                           borrower="Alice Printed", collection_id=20).payload())
        self.assertIn("active on scanner blue", orange.message)
        self.assertEqual([call.args[0] for call in self.client.inventories.call_args_list], [[10]])
        blue.scan("code-1")
        self.wait(lambda: blue.state == "audit_ready")
        orange.set_mode("Assign")
        orange.scan("code-2")
        self.wait(lambda: orange.state == "destination")
        orange.scan(Target("location", "Group A", "B3", collection_id=10).payload())
        self.wait(lambda: orange.state == "idle")
        self.assertEqual(self.client.records["sample:2"].first["current_building"], "B3")
        blue.finish_audit()
        self.wait(lambda: blue.state == "idle")
        self.assertIsNone(self.hub.audit.owner_scanner_id)

    def test_two_scanners_can_operate_for_different_groups(self):
        blue, orange = self.controller("blue"), self.controller("orange")
        blue.scan("code-1")
        orange.scan("code-2")
        self.wait_all("destination")
        blue.scan(Target("location", "Group A", "B2", collection_id=10).payload())
        orange.scan(Target("location", "Group B", "B3", collection_id=20).payload())
        self.wait_all("idle")
        self.assertEqual(self.client.records["sample:1"].first["current_group"], "Group A")
        self.assertEqual(self.client.records["sample:2"].first["current_group"], "Group B")
        self.feedback.assert_any_call("blue", "success")
        self.feedback.assert_any_call("orange", "success")

    def test_inactive_missing_recovery_confirmation_is_routed_without_selecting_it(self):
        self.client.records["sample:1"].first["status"] = "Missing"
        blue = self.controller("blue")
        blue.scan("code-1")
        self.wait(lambda: blue.state == "destination")
        self.hub.select("orange")
        target = Target("location", "Group A", "B2", collection_id=10).payload()
        blue.scan(target)
        self.wait(lambda: blue.awaiting_found is not None)
        self.confirm.assert_called_with("blue", target)
        self.assertEqual(self.hub.active_id, "orange")
        self.assertFalse(self.client.writes)
        blue.reset()
        self.assertIsNone(blue.awaiting_found)

    def test_legacy_configuration_still_creates_one_panel(self):
        config = Config("https://eln.example")
        hub = Hub(Service(self.client, config), Mock(), Mock(), Mock())
        self.addCleanup(hub.executor.shutdown, wait=True)
        self.assertEqual(list(hub.panels), ["default"])
        self.assertTrue(hub.active.ready)


class MultiScannerConfigTests(unittest.TestCase):
    def load(self, definitions, **extra):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "base_url": "https://eln.example", "scanner_backend": "zebra_ssi",
                "scanners": definitions, **extra,
            }))
            return Config.load(path)

    def test_valid_config_and_unknown_or_duplicate_definitions(self):
        scanners = [{"id": "blue", "name": "Blue scanner", "port": "/dev/blue"},
                    {"id": "orange", "name": "Orange scanner", "port": "/dev/orange"}]
        config = self.load(scanners)
        self.assertEqual(config.scanners[1].id, "orange")
        invalid = [
            scanners * 2,
            [scanners[0], {**scanners[1], "port": "/dev/blue"}],
            [{**scanners[0], "id": ""}],
            [{**scanners[0], "name": ""}],
            [{**scanners[0], "port": ""}],
            [{**scanners[0], "unknown": True}],
            "not-an-array",
        ]
        for definitions in invalid:
            with self.subTest(definitions=definitions), self.assertRaises(WorkflowError):
                self.load(definitions)
        with self.assertRaises(WorkflowError):
            self.load(scanners, scanner_backend="keyboard")
        with self.assertRaises(WorkflowError):
            self.load(scanners, scanner_port="/dev/ambiguous")

    def test_multiscanner_example_is_valid(self):
        config = Config.load(Path(__file__).resolve().parents[1] / "config.multiscanner.example.json")
        self.assertEqual([item.id for item in config.scanners], ["blue", "orange"])
        self.assertEqual(config.scanner_backend, "zebra_ssi")

if __name__ == "__main__":
    unittest.main()
