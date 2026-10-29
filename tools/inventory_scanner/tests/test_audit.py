import copy
import unittest
from dataclasses import replace
from unittest.mock import Mock

from tools.inventory_scanner.api import ApiError, PermissionDeniedError, UnverifiedWrite
from tools.inventory_scanner.audit import Audit, Outcome
from tools.inventory_scanner.workflow import Config, Pending, Service, Target, WorkflowError, parse_target
from tools.inventory_scanner.tests.test_workflow import FakeClient, bottle


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.config = Config("https://eln.example")
        self.service = Service(self.client, self.config)
        self.audit = Audit(self.service)
        self.target = Target("location", "Group A", "B1", "204", "C3", collection_id=10)
        for item in (bottle(1), bottle(2), bottle(3, "sequence_based_macromolecule_sample"),
                     bottle(4, cabinet="C7")):
            self.client.records[item.ref.key] = item

    def pending(self, key):
        item = self.client.inventory(next(item.ref for item in self.client.records.values() if item.ref.key == key))
        return Pending(item, "Acetone", key)

    def test_large_collection_retains_only_expected_and_scanned_snapshots(self):
        for number in range(10, 2010):
            item = bottle(number, cabinet="Elsewhere")
            self.client.records[item.ref.key] = item
        self.assertEqual(self.audit.start(self.target, "default"), 3)
        self.assertEqual(len(self.audit.records), 3)
        self.assertEqual(len(self.audit.collection_refs), 2004)
        self.audit.scan(self.pending("sample:4"), "default")
        self.assertFalse(self.audit.records["sample:4"].outside_collection)
        outsider = bottle(10000, cabinet="Elsewhere")
        self.client.records[outsider.ref.key] = outsider
        self.audit.scan(self.pending(outsider.ref.key), "default")
        self.assertTrue(self.audit.records[outsider.ref.key].outside_collection)
        self.assertEqual(len(self.audit.records), 5)
        summary = self.audit.finish("default")
        self.assertEqual(summary, {
            "collection_id": 10, "seen": 2, "outside_collection": 1, "relocated": 2,
            "recovered": 0, "missing": 3, "already_missing": 0, "conflicts": 0,
            "permission_denied": 0, "unresolved": 0,
        })
        self.assertTrue(all(type(value) is int for value in summary.values()))
        self.assertFalse(self.audit.collection_refs)

    def test_unexpected_missing_write_stays_unresolved_without_replay(self):
        self.audit.start(self.target, "default")
        self.client.update_inventory = Mock(side_effect=RuntimeError("write callback failed"))
        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            self.audit.finish("default")
        self.assertEqual(self.audit.records["sample:1"].outcome, Outcome.PENDING)
        self.client.update_inventory.side_effect = PermissionDeniedError("Denied")
        summary = self.audit.finish("default")
        self.assertEqual(summary["unresolved"], 3)
        self.assertEqual(self.client.update_inventory.call_count, 3)
        self.audit.finish("default")
        self.assertEqual(self.client.update_inventory.call_count, 3)

    def test_expected_minus_seen_and_full_preservation(self):
        self.assertEqual(self.audit.start(self.target, "default"), 3)
        before = copy.deepcopy(self.client.records["sample:2"].data)
        self.audit.scan(self.pending("sample:1"), "default")
        self.audit.scan(self.pending("sample:4"), "default")
        summary = self.audit.finish("default")
        self.assertEqual(summary["seen"], 2)
        self.assertEqual(summary["relocated"], 1)
        self.assertEqual(summary["missing"], 2)
        self.assertEqual(summary["unresolved"], 0)
        before[0]["status"] = "Missing"
        self.assertEqual(self.client.records["sample:2"].data, before)
        self.assertEqual(self.client.records["sample:4"].first["current_cabinet"], "C3")
        self.assertFalse(self.audit.records)

    def test_group_from_location_label_controls_baseline_and_relocation(self):
        self.client.records["sample:1"].first["current_group"] = "Group B"
        target = replace(self.target, group="Group B", collection_id=20)
        self.assertEqual(self.audit.start(target, "default"), 1)
        self.assertEqual(self.audit.target.group, "Group B")
        self.audit.scan(self.pending("sample:4"), "default")
        self.assertEqual(self.client.records["sample:4"].first["current_group"], "Group B")
        summary = self.audit.finish("default")
        self.assertEqual(summary["missing"], 1)
        self.assertEqual(self.client.records["sample:1"].first["status"], "Missing")
        self.assertEqual(self.client.records["sample:2"].first["status"], "Available")

    def test_permission_failure_on_scanned_bottle_is_retained_in_memory_and_blocks_finish(self):
        self.audit.start(self.target, "default")
        self.client.update_inventory = Mock(side_effect=PermissionDeniedError("Write denied: API token permissions."))
        with self.assertRaises(PermissionDeniedError):
            self.audit.scan(self.pending("sample:4"), "default")
        row = self.audit.records["sample:4"]
        self.assertEqual((row.seen, row.outcome), (1, "permission_denied"))
        self.assertIn("API token permissions", row.detail)
        with self.assertRaises(WorkflowError):
            self.audit.finish("default")
        self.assertEqual(self.audit.summary()["permission_denied"], 1)
        self.client.update_inventory.assert_called_once()

    def test_seen_duplicate_ignored(self):
        self.audit.start(self.target, "default")
        self.audit.scan(self.pending("sample:4"), "default")
        writes = len(self.client.writes)
        self.assertIn("Already", self.audit.scan(self.pending("sample:4"), "default"))
        self.assertEqual(len(self.client.writes), writes)
        self.assertEqual(self.audit.summary()["seen"], 1)

    def test_cancellation_no_missing_but_keeps_relocation(self):
        self.audit.start(self.target, "default")
        self.audit.scan(self.pending("sample:4"), "default")
        self.audit.cancel("default")
        self.assertTrue(all(item.first["status"] == "Available" for item in self.client.records.values()))
        self.assertEqual(self.client.records["sample:4"].first["current_cabinet"], "C3")
        self.assertFalse(self.audit.active)
        self.assertFalse(self.audit.records)

    def test_late_recovery_at_another_cabinet(self):
        self.audit.start(self.target, "default")
        self.audit.finish("default")
        self.assertEqual(self.client.records["sample:2"].first["status"], "Missing")
        self.audit.start(Target("location", "Group A", "B1", "204", "C7", collection_id=10), "default")
        self.audit.scan(self.pending("sample:2"), "default")
        self.assertEqual(self.client.records["sample:2"].first["status"], "Available")
        self.assertEqual(self.client.records["sample:2"].first["current_cabinet"], "C7")
        self.assertEqual(self.audit.summary()["recovered"], 1)

    def test_preserve_out_of_stock_on_physical_scan(self):
        self.client.records["sample:1"].first["status"] = "Out of stock"
        self.audit.start(self.target, "default")
        self.audit.scan(self.pending("sample:1"), "default")
        self.assertEqual(self.client.records["sample:1"].first["status"], "Out of stock")

    def test_concurrent_relocation_not_overwritten(self):
        self.audit.start(self.target, "default")
        self.client.records["sample:2"].first["current_cabinet"] = "C9"
        summary = self.audit.finish("default")
        self.assertEqual(summary["conflicts"], 1)
        self.assertEqual(summary["unresolved"], 1)
        self.assertEqual(self.client.records["sample:2"].first["status"], "Available")
        self.assertEqual(self.audit.summary()["unresolved"], 1)

    def test_scanned_write_failure_is_seen_but_blocks_finish(self):
        self.audit.start(self.target, "default")
        self.client.update_inventory = Mock(side_effect=UnverifiedWrite("Not verified"))
        with self.assertRaises(UnverifiedWrite):
            self.audit.scan(self.pending("sample:4"), "default")
        self.assertEqual(self.audit.summary()["seen"], 1)
        with self.assertRaises(WorkflowError):
            self.audit.finish("default")
        self.assertTrue(all(item.first["status"] == "Available" for item in self.client.records.values()))

    def test_scanned_outsider_recorded_before_write_and_duplicates_ignored(self):
        self.audit.start(self.target, "default")
        outsider = bottle(999, "sequence_based_macromolecule_sample", "Missing", cabinet="Elsewhere")
        self.client.records[outsider.ref.key] = outsider
        self.client.refs["outside"] = outsider.ref
        pending = self.service.load("outside")
        original = copy.deepcopy(outsider.data)
        update = self.client.update_inventory

        def recorded_before_write(latest, data):
            row = self.audit.records[outsider.ref.key]
            self.assertEqual((row.expected, row.seen, row.outside_collection), (0, 1, 1))
            self.assertEqual(row.outcome, "pending")
            self.assertNotIn("amount", row.baseline)
            return update(latest, data)

        self.client.update_inventory = Mock(side_effect=recorded_before_write)
        message = self.audit.scan(pending, "default")
        self.assertIn("memberships unchanged", message)
        saved = self.client.records[outsider.ref.key]
        self.assertEqual(saved.first["current_cabinet"], "C3")
        self.assertEqual(self.client.records[outsider.ref.key].first["status"], "Available")
        self.assertEqual(saved.first["amount"], original[0]["amount"])
        self.assertEqual(saved.first["host_building"], original[0]["host_building"])
        self.assertEqual(saved.data[1:], original[1:])
        self.assertTrue(saved.first["important_notes"].startswith("Keep dry\nFound "))
        self.assertIn("Already", self.audit.scan(self.service.load("outside"), "default"))
        self.client.update_inventory.assert_called_once()
        self.client.update_inventory = update
        summary = self.audit.finish("default")
        self.assertEqual(summary["outside_collection"], 1)
        self.assertEqual(summary["recovered"], 1)
        self.assertEqual(summary["missing"], 3)
        self.assertEqual(summary["unresolved"], 0)
        self.assertEqual(saved.first["status"], "Available")

    def test_unseen_outsiders_never_enter_expected_missing_set(self):
        scoped = [copy.deepcopy(item) for item in self.client.records.values()]
        self.client.inventories = Mock(return_value=scoped)
        outsider = bottle(999)
        self.client.records[outsider.ref.key] = outsider
        self.assertEqual(self.audit.start(self.target, "default"), 3)
        self.assertEqual(self.audit.finish("default")["missing"], 3)
        self.assertEqual(self.client.records[outsider.ref.key].first["status"], "Available")
        self.assertNotIn(outsider.ref.key, self.audit.records)

    def test_failed_external_write_is_seen_and_never_becomes_missing(self):
        self.audit.start(self.target, "default")
        outsider = bottle(999, cabinet="Elsewhere")
        self.client.records[outsider.ref.key] = outsider
        self.client.update_inventory = Mock(side_effect=UnverifiedWrite("Not verified"))
        with self.assertRaises(UnverifiedWrite):
            self.audit.scan(self.pending(outsider.ref.key), "default")
        row = self.audit.records[outsider.ref.key]
        self.assertEqual((row.expected, row.seen, row.outside_collection), (0, 1, 1))
        self.assertEqual(row.outcome, "unverified")
        with self.assertRaisesRegex(WorkflowError, "failed/unverified"):
            self.audit.finish("default")
        self.client.update_inventory.assert_called_once()
        self.assertTrue(all(item.first["status"] == "Available" for item in self.client.records.values()))

    def test_outsider_already_at_location_is_seen_without_write(self):
        self.audit.start(self.target, "default")
        outsider = bottle(999)
        self.client.records[outsider.ref.key] = outsider
        self.audit.scan(self.pending(outsider.ref.key), "default")
        self.assertFalse(self.client.writes)
        self.assertEqual(self.audit.summary()["outside_collection"], 1)
        self.assertEqual(self.audit.summary()["relocated"], 0)

    def test_audit_scope_comes_only_from_each_location_label(self):
        by_collection = {10: [bottle(1)], 20: [bottle(2, "sequence_based_macromolecule_sample")]}
        self.client.inventories = Mock(side_effect=lambda ids: copy.deepcopy(by_collection[ids[0]]))
        for collection_id, expected_ref in ((10, "sample:1"), (20, "sequence_based_macromolecule_sample:2")):
            with self.subTest(collection_id=collection_id):
                self.assertEqual(self.audit.start(replace(self.target, collection_id=collection_id), "default"), 1)
                self.assertEqual(list(self.audit.records), [expected_ref])
                self.assertEqual(self.audit.summary()["collection_id"], collection_id)
                self.assertEqual(parse_target(self.audit.target.payload()).collection_id, collection_id)
                self.audit.cancel("default")
        self.assertEqual([call.args[0] for call in self.client.inventories.call_args_list], [[10], [20]])

    def test_failed_load_blocks_finish_until_same_code_scanned(self):
        self.audit.start(self.target, "default")
        pending = self.pending("sample:1")
        self.audit.failed_scan(pending.code, "default")
        with self.assertRaises(WorkflowError):
            self.audit.finish("default")
        self.audit.scan(pending, "default")
        self.assertEqual(self.audit.finish("default")["missing"], 2)

    def test_failed_duplicate_load_can_be_resolved_by_rescanning(self):
        self.audit.start(self.target, "default")
        pending = self.pending("sample:1")
        self.audit.scan(pending, "default")
        self.audit.failed_scan(pending.code, "default")
        with self.assertRaises(WorkflowError):
            self.audit.finish("default")
        self.audit.scan(pending, "default")
        self.assertEqual(self.audit.finish("default")["missing"], 2)

    def test_exactly_thirty_scans_allowed_but_not_thirty_one(self):
        self.client.records = {item.ref.key: item for item in
                               (bottle(i, cabinet="Elsewhere") for i in range(1, 32))}
        self.audit.start(self.target, "default")
        for number in range(1, 31):
            self.audit.scan(self.pending(f"sample:{number}"), "default")
        self.assertEqual(self.audit.summary()["seen"], 30)
        with self.assertRaises(WorkflowError):
            self.audit.scan(self.pending("sample:31"), "default")
        self.assertEqual(self.audit.finish("default")["unresolved"], 0)

    def test_external_bottles_share_the_thirty_bottle_scan_limit(self):
        self.client.inventories = Mock(return_value=[])
        self.client.records = {item.ref.key: item for item in
                               (bottle(i, cabinet="Elsewhere") for i in range(1, 32))}
        self.audit.start(self.target, "default")
        for number in range(1, 31):
            self.audit.scan(self.pending(f"sample:{number}"), "default")
        with self.assertRaisesRegex(WorkflowError, "30 bottles"):
            self.audit.scan(self.pending("sample:31"), "default")
        self.assertEqual(len(self.audit.records), 30)
        self.assertIn("Already", self.audit.scan(self.pending("sample:1"), "default"))
        summary = self.audit.finish("default")
        self.assertEqual(summary["outside_collection"], 30)
        self.assertEqual(summary["missing"], 0)
        self.assertEqual(summary["unresolved"], 0)

    def test_incomplete_baseline_does_not_start(self):
        self.client.inventories = Mock(side_effect=ApiError("Page incomplete"))
        with self.assertRaises(ApiError):
            self.audit.start(self.target, "default")
        self.assertFalse(self.audit.active)
        self.assertFalse(self.client.writes)

    def test_inaccessible_collection_does_not_start(self):
        self.client.inventories = Mock(side_effect=ApiError("Collection unavailable or detail access insufficient"))
        with self.assertRaisesRegex(ApiError, "Collection unavailable"):
            self.audit.start(self.target, "default")
        self.assertFalse(self.audit.active)
        self.assertFalse(self.client.writes)
        self.client.inventories.assert_called_once_with([10])

    def test_only_locations_with_collection_scope_can_start(self):
        for target in (replace(self.target, collection_id=None), Target("person", "Group A", borrower="Alice Current")):
            with self.subTest(target=target), self.assertRaises(WorkflowError):
                self.audit.start(target, "default")
        self.assertFalse(self.audit.active)
        self.assertFalse(self.client.writes)

    def test_person_and_location_labels_have_identical_audit_semantics(self):
        for kind in ("person", "location"):
            with self.subTest(kind=kind):
                for item in self.client.records.values():
                    item.first.update(current_building="", current_room="", current_cabinet="",
                                      current_group="Printed Group", borrowed_by="Alice Printed", status="Available")
                target = Target(kind, "Printed Group", borrower="Alice Printed", collection_id=10)
                self.assertEqual(self.audit.start(target, "default"), 4)
                self.audit.scan(self.pending("sample:1"), "default")
                self.assertEqual(self.audit.summary()["relocated"], 0)
                self.assertEqual(self.audit.finish("default")["missing"], 3)
                self.audit.cancel("default")

    def test_expected_bottle_limit(self):
        self.client.records = {item.ref.key: item for item in (bottle(i) for i in range(1, 32))}
        with self.assertRaises(WorkflowError):
            self.audit.start(self.target, "default")
        self.assertFalse(self.audit.active)

    def test_one_audit_object_rejects_a_second_start_until_cancelled(self):
        self.audit.start(self.target, "default")
        with self.assertRaisesRegex(WorkflowError, "Finish or cancel"):
            self.audit.start(self.target, "default")
        self.audit.cancel("default")
        self.assertEqual(self.audit.start(self.target, "default"), 3)
        self.audit.cancel("default")

    def test_only_the_starting_scanner_can_continue_or_cancel_the_audit(self):
        self.audit.start(self.target, "blue")
        with self.assertRaisesRegex(WorkflowError, "active on scanner blue"):
            self.audit.scan(self.pending("sample:1"), "orange")
        with self.assertRaisesRegex(WorkflowError, "active on scanner blue"):
            self.audit.finish("orange")
        with self.assertRaisesRegex(WorkflowError, "active on scanner blue"):
            self.audit.cancel("orange")
        self.assertTrue(self.audit.belongs_to("blue"))
        self.audit.cancel("blue")

    def test_second_finish_does_not_replay_unresolved_mutations(self):
        self.audit.start(self.target, "default")
        self.client.update_inventory = Mock(side_effect=ApiError("Write failed"))
        first = self.audit.finish("default")
        self.assertEqual(first["unresolved"], 3)
        count = self.client.update_inventory.call_count
        self.audit.finish("default")
        self.assertEqual(self.client.update_inventory.call_count, count)


if __name__ == "__main__":
    unittest.main()
