import copy
import unittest
from concurrent.futures import Future
from unittest.mock import Mock

from tools.inventory_scanner.api import ApiError, AuthenticationError, PermissionDeniedError, UnverifiedWrite
from tools.inventory_scanner.audit import Audit
from tools.inventory_scanner.controller import Controller, MessageLevel, Mode, State
from tools.inventory_scanner.workflow import Config, Service, Target, WorkflowError
from tools.inventory_scanner.tests.test_workflow import FakeClient, bottle


class ControlledExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, task):
        future = Future()
        self.jobs.append((task, future))
        return future

    def run(self):
        task, future = self.jobs.pop(0)
        try:
            result = task()
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def shutdown(self, wait=False):
        pass


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        item = bottle()
        self.client.records[item.ref.key] = item
        self.code = "123456789012"
        self.client.refs[self.code] = item.ref
        self.target = Target("location", "Group A", "B2", collection_id=10).payload()
        config = Config("https://eln.example")
        self.service = Service(self.client, config)
        self.audit = Audit(self.service)
        self.executor = ControlledExecutor()
        self.now = 0
        self.changed, self.confirm = Mock(), Mock()
        self.model = Controller(self.service, self.audit, self.changed, self.confirm,
                                executor=self.executor, clock=lambda: self.now)
        self.model.connect()
        self.complete()

    def complete(self):
        self.executor.run()
        self.model.poll()

    def test_buffered_cross_group_destination_uses_its_own_group(self):
        self.model.scan(self.code)
        self.model.scan(Target("location", "Group B", "B2", collection_id=20).payload())
        self.complete()
        self.complete()
        self.assertEqual(self.model.state, "idle")
        self.assertEqual(self.client.records["sample:1"].first["current_group"], "Group B")
        self.assertIn("Group: Group B", self.model.details)
        self.assertEqual(self.model.account_name, "Account 1000")

    def test_write_permission_error_is_visible_and_sounds_error_not_success(self):
        self.model.feedback = Mock()
        self.model.scan(self.code)
        self.complete()
        self.client.update_inventory = Mock(side_effect=PermissionDeniedError(
            "Write denied: API token lacks write permission. Check collection permissions."
        ))
        self.model.scan(self.target)
        self.complete()
        self.assertEqual(self.model.state, "destination")
        self.assertIn("API token lacks write permission", self.model.message)
        self.assertFalse(self.model.success)
        self.model.feedback.assert_called_with("error")
        self.client.update_inventory.assert_called_once()
        self.assertEqual(self.client.records["sample:1"].first["current_building"], "B1")

    def test_audit_finish_reports_permission_failures_and_stays_incomplete(self):
        self.model.set_mode("Audit")
        self.model.scan(Target("location", "Group A", "B1", "204", "C3", collection_id=10).payload())
        self.complete()
        self.client.update_inventory = Mock(side_effect=PermissionDeniedError("API token lacks permission."))
        self.model.feedback = Mock()
        self.model.finish_audit()
        self.complete()
        self.assertEqual(self.model.state, "audit_ready")
        self.assertIn("API token lacks write permission", self.model.message)
        self.assertEqual(self.audit.summary()["permission_denied"], 1)
        self.assertEqual(self.audit.summary()["unresolved"], 1)
        self.model.feedback.assert_called_with("error")
        self.model.finish_audit()
        self.complete()
        self.client.update_inventory.assert_called_once()

    def test_two_fast_scans_buffer_exactly_one_destination(self):
        self.model.scan(self.code)
        self.model.scan(self.target)
        self.model.scan(Target("location", "Group A", "B3", collection_id=10).payload())
        self.assertIn("already buffered", self.model.message)
        self.complete()
        self.assertEqual(self.model.state, "saving")
        self.complete()
        self.assertEqual(self.model.state, "idle")
        self.assertEqual(self.client.records["sample:1"].first["current_building"], "B2")
        self.assertEqual(len(self.client.writes), 1)
        self.model.scan(self.target)
        self.complete()
        self.assertFalse(self.executor.jobs)
        self.assertEqual(len(self.client.writes), 1)

    def test_feedback_is_based_on_recognition_and_verified_save_not_receipt(self):
        sounds = []
        self.model.feedback = sounds.append
        self.model.scan(self.code)
        self.assertEqual(sounds, [])
        self.model.scan(self.target)
        self.assertEqual(sounds, [])
        self.complete()
        self.assertEqual(sounds, ["recognized"])
        self.complete()
        self.assertEqual(sounds, ["recognized", "success"])

    def test_unverified_write_has_error_sound_never_success(self):
        sounds = []
        self.model.feedback = sounds.append
        self.model.scan(self.code)
        self.complete()
        self.client.update_inventory = Mock(side_effect=UnverifiedWrite("Readback failed"))
        self.model.scan(self.target)
        self.complete()
        self.assertEqual(sounds, ["recognized", "error"])
        self.assertEqual(self.model.state, "idle")

    def test_unverified_write_after_server_mutation_reloads_saved_inventory(self):
        self.model.scan(self.code)
        self.complete()
        update = self.client.update_inventory

        def unverified_after_save(latest, data):
            update(latest, data)
            raise UnverifiedWrite("Readback failed")

        self.client.update_inventory = Mock(side_effect=unverified_after_save)
        self.model.scan(self.target)
        self.complete()
        self.assertEqual(self.model.state, "idle")
        self.assertEqual(len(self.client.writes), 1)
        self.assertIn("may have reached", self.model.message)
        self.model.scan(self.target)
        self.complete()
        self.assertEqual(len(self.client.writes), 1)
        self.model.scan(self.code)
        self.complete()
        self.assertEqual(self.model.pending.inventory.first["current_building"], "B2")
        self.assertEqual(len(self.client.writes), 1)

    def test_rejected_scan_and_lookup_have_distinct_feedback(self):
        sounds = []
        self.model.feedback = sounds.append
        self.model.scan("")
        self.assertEqual(sounds, ["error"])
        self.model.set_mode("Lookup")
        self.model.scan(self.code)
        self.complete()
        self.assertEqual(sounds, ["error", "recognized"])
        self.assertFalse(self.client.writes)

    def test_reset_discards_stale_lookup_and_buffer(self):
        self.model.scan(self.code)
        self.model.scan(self.target)
        self.model.reset()
        self.complete()
        self.assertEqual(self.model.state, "idle")
        self.assertIsNone(self.model.pending)
        self.assertFalse(self.client.writes)

    def test_reset_during_load_allows_immediate_fresh_scan(self):
        self.model.scan(self.code)
        self.model.scan(self.target)
        self.model.reset()
        self.model.scan(self.code)
        self.assertEqual(len(self.executor.jobs), 2)
        self.complete()
        self.assertEqual(self.model.state, State.LOADING)
        self.complete()
        self.assertEqual(self.model.state, State.DESTINATION)
        self.assertIsNone(self.model.buffer)
        self.assertFalse(self.client.writes)

    def test_states_and_visual_levels_are_explicit(self):
        self.assertIsInstance(self.model.state, State)
        self.assertIsInstance(self.model.mode, Mode)
        self.model.notify(self.model.instruction())
        self.assertEqual(self.model.level, MessageLevel.INSTRUCTION)
        self.model.scan(self.code)
        self.assertEqual(self.model.level, MessageLevel.WORKING)
        self.complete()
        self.model.notify("Review this warning.", level=MessageLevel.WARNING)
        self.assertEqual(self.model.level, MessageLevel.WARNING)
        self.assertFalse(self.model.success)
        self.model.scan(self.target)
        self.complete()
        self.assertEqual(self.model.level, MessageLevel.SUCCESS)

    def test_standalone_controller_owns_and_closes_its_worker(self):
        model = Controller(self.service, self.audit, Mock(), Mock())
        with unittest.mock.patch.object(model.executor, "shutdown",
                                        wraps=model.executor.shutdown) as shutdown:
            model.close()
            model.close()
        shutdown.assert_called_once_with(wait=False)

    def test_audit_unexpected_scan_failure_can_be_reconciled_by_rescan(self):
        self.model.set_mode("Audit")
        self.model.scan(Target(building="B1", room="204", cabinet="C3",
                               group="Group A", collection_id=10).payload())
        self.complete()
        original = self.service.assign
        self.service.assign = Mock(side_effect=RuntimeError("audit write failed"))
        self.model.scan(self.code)
        self.complete()
        with self.assertLogs("tools.inventory_scanner.controller", level="ERROR"):
            self.complete()
        self.assertEqual(self.model.state, State.AUDIT_READY)
        self.assertEqual(self.audit.summary()["unresolved"], 1)
        self.assertIsNone(self.model.pending)
        self.service.assign = original
        self.model.scan(self.code)
        self.complete()
        self.complete()
        self.assertEqual(self.audit.summary()["unresolved"], 0)
        self.model.finish_audit()
        self.complete()
        self.assertEqual(self.model.state, State.IDLE)

    def test_duplicate_chemical_suppressed(self):
        self.model.scan(self.code)
        self.model.scan(self.code)
        self.assertIsNone(self.model.buffer)
        self.assertEqual(len(self.executor.jobs), 1)
        self.complete()
        self.model.scan(self.code)
        self.assertEqual(self.model.state, "destination")
        self.assertFalse(self.client.writes)

    def test_lookup_missing_never_writes(self):
        self.client.records["sample:1"].first["status"] = "Missing"
        self.model.set_mode("Lookup")
        self.model.scan(self.code)
        self.complete()
        self.assertEqual(self.model.state, "idle")
        self.assertIn("last recorded", self.model.details)
        self.assertFalse(self.client.writes)
        self.confirm.assert_not_called()

    def test_missing_confirmation_and_cancellation_no_write(self):
        self.client.records["sample:1"].first["status"] = "Missing"
        self.model.scan(self.code)
        self.complete()
        self.model.scan(self.target)
        self.complete()
        self.assertEqual(self.model.state, "destination")
        self.confirm.assert_called_once_with(self.target)
        self.assertFalse(self.client.writes)
        self.model.assign(self.target, found=True)
        self.complete()
        self.assertEqual(self.client.records["sample:1"].first["status"], "Available")

    def test_timeout_does_not_cancel_writes(self):
        self.model.scan(self.code)
        self.complete()
        self.now = 61
        self.model.tick()
        self.assertEqual(self.model.state, "idle")
        self.assertFalse(self.client.writes)
        self.model.scan(self.code)
        self.complete()
        self.model.scan(self.target)
        self.now = 1000
        self.model.tick()
        self.assertEqual(self.model.state, "saving")
        self.assertFalse(self.model.reset())
        self.complete()
        self.assertEqual(len(self.client.writes), 1)

    def test_conflict_refreshes_pending_and_requires_target_rescan(self):
        self.model.scan(self.code)
        self.complete()
        self.client.records["sample:1"].first["current_cabinet"] = "C9"
        self.model.scan(self.target)
        self.complete()
        self.assertEqual(self.model.state, "destination")
        self.assertEqual(self.model.pending.inventory.first["current_cabinet"], "C9")
        self.assertFalse(self.client.writes)
        self.model.scan(self.target)
        self.complete()
        self.assertEqual(len(self.client.writes), 1)

    def test_uncertain_write_accepts_fresh_pair_without_reset_or_replay(self):
        self.model.scan(self.code)
        self.complete()
        self.client.update_inventory = Mock(side_effect=UnverifiedWrite("Not verified"))
        self.model.scan(self.target)
        self.complete()
        self.assertEqual(self.model.state, "idle")
        self.assertIn("may have reached", self.model.message)
        self.assertIsNone(self.model.pending)
        self.assertIsNone(self.model.awaiting_found)
        self.assertIsNone(self.model.buffer)
        self.client.update_inventory.assert_called_once()
        self.model.scan(self.code)
        self.complete()
        self.assertEqual(self.model.state, "destination")
        self.assertEqual(self.client.update_inventory.call_count, 1)

    def test_unexpected_worker_failure_allows_new_scan(self):
        original = self.service.load
        self.service.load = Mock(side_effect=RuntimeError("unexpected lookup failure"))
        self.model.scan(self.code)
        with self.assertLogs("tools.inventory_scanner.controller", level="ERROR"):
            self.complete()
        self.assertEqual(self.model.state, "idle")
        self.assertIn("unexpected lookup failure", self.model.message)
        self.assertIsNone(self.model.pending)
        self.service.load = original
        self.model.scan(self.code)
        self.complete()
        self.assertEqual(self.model.state, "destination")
        self.assertFalse(self.client.writes)

    def test_unexpected_completion_failure_does_not_replay_saved_write(self):
        self.model.scan(self.code)
        self.complete()
        original = self.model.assigned
        self.model.assigned = Mock(side_effect=RuntimeError("display failed"))
        self.model.scan(self.target)
        with self.assertLogs("tools.inventory_scanner.controller", level="ERROR"):
            self.complete()
        self.assertEqual(self.model.state, "idle")
        self.assertIn("may have reached", self.model.message)
        self.assertEqual(len(self.client.writes), 1)
        self.model.assigned = original
        self.model.scan(self.code)
        self.complete()
        self.assertEqual(self.model.pending.inventory.first["current_building"], "B2")
        self.assertEqual(len(self.client.writes), 1)

    def test_invalid_unicode_is_a_visible_scan_error(self):
        self.model.scan("\ud800")
        self.assertIn("UTF-8", self.model.message)
        self.assertFalse(self.executor.jobs)

    def test_authentication_failure_blocks_writes(self):
        self.client.authenticate = Mock(side_effect=AuthenticationError("Token expired"))
        self.model.connect()
        self.complete()
        self.assertEqual(self.model.state, "blocked")
        self.model.scan(self.code)
        self.assertFalse(self.executor.jobs)

    def test_failed_audit_load_durably_blocks_finish(self):
        self.model.set_mode("Audit")
        self.model.scan(Target("location", "Group A", "B1", "204", "C3", collection_id=10).payload())
        self.complete()
        self.service.load = Mock(side_effect=ApiError("Lookup failed"))
        self.model.scan(self.code)
        self.complete()
        self.model.finish_audit()
        self.complete()
        self.assertIn("failed to load", self.model.message)
        self.assertFalse(self.client.writes)

    def test_destination_scan_during_audit_does_not_poison_baseline(self):
        self.model.set_mode("Audit")
        location = Target("location", "Group A", "B1", "204", "C3", collection_id=10).payload()
        self.model.scan(location)
        self.complete()
        self.assertIn("Collection 10", self.model.details)
        self.model.scan(location)
        self.assertIn("already selected", self.model.message)
        self.assertFalse(self.executor.jobs)
        self.model.scan(self.code)
        self.complete()
        self.complete()
        self.model.finish_audit()
        self.complete()
        self.assertEqual(self.model.state, "idle")

    def test_busy_scans_do_not_create_more_writes(self):
        self.model.scan(self.code)
        self.complete()
        self.model.scan(self.target)
        self.model.scan(self.code)
        self.assertIn("not accepted", self.model.message)
        self.assertEqual(len(self.executor.jobs), 1)
        with self.assertRaises(WorkflowError):
            self.model.close()
        self.complete()


if __name__ == "__main__":
    unittest.main()
