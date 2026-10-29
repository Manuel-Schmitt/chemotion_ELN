import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tools.inventory_scanner.api import AuthenticationError, Inventory, Ref
from tools.inventory_scanner.workflow import (
    MAX_CODE_BYTES, Config, Conflict, FoundConfirmation, Pending, Service, Target,
    WorkflowError, at_location, parse_target,
)


class FakeClient:
    def __init__(self):
        self.records = {}
        self.writes = []
        self.user = {"id": 1000, "type": "Person"}
        self.refs = {}

    def authenticate(self):
        return copy.deepcopy(self.user)

    def inventory(self, ref):
        return copy.deepcopy(self.records[ref.key])

    def update_inventory(self, latest, data):
        self.writes.append((latest.ref, copy.deepcopy(data)))
        item = Inventory(latest.ref, latest.id, copy.deepcopy(data))
        self.records[latest.ref.key] = item
        return copy.deepcopy(item)

    def inventories(self, _):
        return [copy.deepcopy(item) for item in self.records.values()]

    def resolve(self, code):
        return self.refs[code]

    def describe(self, ref):
        return f"Acetone / {ref.key}"


def bottle(number=1, kind="sample", status="Available", cabinet="C3"):
    return Inventory(Ref(kind, number), number + 100, [{
        "current_building": "B1", "current_room": "204", "current_cabinet": cabinet,
        "current_group": "Group A", "borrowed_by": "", "status": status,
        "host_building": "Home", "amount": {"value": 50, "unit": "g"},
        "important_notes": "Keep dry", "nested": {"x": [1, 2]},
    }, {"extra": ["must", "survive"]}])


class LabelTests(unittest.TestCase):
    def test_round_trip_both_targets_and_unicode(self):
        for target in (Target("location", "Group A", "Gebäude", "204", "C3", collection_id=10),
                       Target("person", "Group A", borrower="Alice | 100% Current")):
            with self.subTest(target=target):
                self.assertEqual(parse_target(target.payload()), target)

    def test_building_only_and_room_only(self):
        for kind in ("location", "person"):
            for field in ("building", "room", "cabinet", "group", "borrower"):
                with self.subTest(kind=kind, field=field):
                    target = parse_target(Target(kind, **{field: "Supplied text"}).payload())
                    self.assertEqual(getattr(target, field), "Supplied text")
                    self.assertEqual(sum(bool(value) for value in target.assignment.values()), 1)

    def test_reject_invalid_shapes(self):
        cases = [
            "ci1|B1|", "ci1|B1|||||10|extra", "ci1|B1%2F1|||||",
            "ci1|B1%7c1|||||", "ci1|B1\n||||", "ci1||||||",
            "ci1|B1|||||0", "ci1|B1|||||-1", "ci1|B1|||||01",
            "chemotion-inventory:v1:{\"type\":\"location\",\"building\":\"B1\"}",
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(WorkflowError):
                parse_target(data)

    def test_versions_and_length_boundaries(self):
        with self.assertRaises(WorkflowError):
            parse_target("ci2|B1|||||")
        target = Target("location", "Group A", "B" * 100, "R" * 100, "C" * 100, collection_id=10)
        self.assertEqual(parse_target(target.payload()), target)
        with self.assertRaises(WorkflowError):
            Target("location", "Group A", "B" * 101, collection_id=10).payload()
        with self.assertRaises(WorkflowError):
            parse_target("x" * (MAX_CODE_BYTES + 1))

    def test_collection_id_is_optional_for_assignment_and_strict_when_supplied(self):
        for kind in ("location", "person"):
            target = Target(kind, building="B1")
            self.assertIsNone(parse_target(target.payload()).collection_id)
            with_id = Target(kind, building="B1", collection_id=10)
            self.assertEqual(parse_target(with_id.payload()).collection_id, 10)
            for value in ("0", "-1", "ten", "10.0", "01"):
                with self.subTest(kind=kind, value=value), self.assertRaisesRegex(WorkflowError, "Collection ID"):
                    parse_target(f"ci1|B1|||||{value}")

    def test_old_id_labels_require_reprinting_without_name_lookup(self):
        for label in (
            'chemotion-inventory:v1:{"type":"person","group_id":42,"person_id":7}',
            'chemotion-inventory:v1:{"type":"location","group_id":42,"building":"B1"}',
        ):
            with self.subTest(label=label), self.assertRaisesRegex(WorkflowError, "Reprint.*ci1"):
                parse_target(label)

    def test_all_five_fields_have_the_same_strict_text_validation(self):
        for kind in ("location", "person"):
            for field in ("building", "room", "cabinet", "group", "borrower"):
                for value in (None, True, 123, [], {}, " ", " trim", "line\nbreak", "\x7f", "\ud800", "x" * 101):
                    with self.subTest(kind=kind, field=field, value=value), self.assertRaises(WorkflowError):
                        values = {name: "" for name in ("building", "room", "cabinet", "group", "borrower")}
                        values.update(building="B1")
                        values[field] = value
                        Target(kind, **values).payload()
        for target in (Target("person", group="\ud800"), Target("location", borrower="\ud800")):
            with self.subTest(target=target), self.assertRaisesRegex(WorkflowError, "trimmed text"):
                target.payload()

    def test_labels_from_different_groups_are_accepted_without_station_scope(self):
        for group in ("Group A", "Group B", ""):
            target = Target("location", group, "B1", collection_id=10)
            self.assertEqual(parse_target(target.payload()).group, group)


class ConfigTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data))
            return Config.load(path)

    def test_valid_config_has_no_audit_storage_setting(self):
        config = self.load({"base_url": "https://eln.example/subpath"})
        self.assertTrue(config.fullscreen)

    def test_audit_directory_configuration_is_rejected(self):
        with self.assertRaisesRegex(WorkflowError, "Remove audit_directory.*memory only"):
            self.load({"base_url": "https://eln.example", "audit_directory": "records"})

    def test_old_collection_configuration_requires_migration(self):
        for value in ([], [10, 20], None):
            with self.subTest(value=value), self.assertRaisesRegex(
                    WorkflowError, "Remove inventory_collection_ids.*reprint location labels"):
                self.load({"base_url": "https://eln.example",
                           "inventory_collection_ids": value})

    def test_old_fixed_group_configuration_requires_removal(self):
        with self.assertRaisesRegex(WorkflowError, "Remove group_id.*no longer group-locked"):
            self.load({"base_url": "https://eln.example", "group_id": 42})

    def test_invalid_config_never_defaults_silently(self):
        for key, value in (
            ("base_url", "http://eln.example"), ("base_url", "https://user:secret@eln.example"),
            ("base_url", "https://eln.example?token=secret"), ("group_id", True),
            ("timeout", 0), ("timeout", float("inf")), ("pair_timeout", "60"),
            ("fullscreen", "true"), ("station_name", ""), ("unknown", 1),
            ("scanner_backend", "hid"), ("scanner_port", " port "),
            ("scanner_baudrate", True), ("scanner_baudrate", 123),
            ("scanner_timeout", 0), ("scanner_timeout", float("nan")),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(WorkflowError):
                self.load({"base_url": "https://eln.example", key: value})

    def test_zebra_configuration_requires_port_and_preserves_keyboard_default(self):
        self.assertEqual(self.load({"base_url": "https://eln.example"}).scanner_backend,
                         "keyboard")
        with self.assertRaises(WorkflowError):
            self.load({"base_url": "https://eln.example", "scanner_backend": "zebra_ssi"})
        config = self.load({"base_url": "https://eln.example",
                            "scanner_backend": "zebra_ssi", "scanner_port": "/dev/serial/by-id/test"})
        self.assertEqual(config.scanner_port, "/dev/serial/by-id/test")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.config = Config("https://eln.example", station_name="Station A")
        self.service = Service(self.client, self.config)
        self.item = bottle()
        self.client.records[self.item.ref.key] = self.item
        self.client.refs["123456789012"] = self.item.ref
        self.pending = Pending(copy.deepcopy(self.item), "Acetone", "123456789012")

    def test_load_read_only_and_destination_first_rejected(self):
        result = self.service.load(self.pending.code)
        self.assertEqual(result.inventory.ref, self.item.ref)
        self.assertIn("Acetone", result.label)
        self.assertFalse(self.client.writes)
        with self.assertRaises(WorkflowError):
            self.service.load(Target("location", "Group A", "B1", collection_id=10).payload())

    def test_location_preserves_full_latest_inventory_for_both_types(self):
        for kind in ("sample", "sequence_based_macromolecule_sample"):
            item = bottle(kind=kind)
            pending = Pending(copy.deepcopy(item), "Acetone", "code")
            item.first["new_unrelated"] = {"latest": True}
            item.first["borrowed_by"] = ""
            self.client.records[item.ref.key] = item
            result, _ = self.service.assign(pending, Target("location", "Group A", "B2", collection_id=10))
            self.assertEqual(result.first["current_room"], "")
            self.assertEqual(result.first["current_cabinet"], "")
            self.assertEqual(result.first["borrowed_by"], "")
            self.assertEqual(result.first["host_building"], "Home")
            self.assertEqual(result.first["amount"], item.first["amount"])
            self.assertEqual(result.first["new_unrelated"], {"latest": True})
            self.assertEqual(result.data[1], item.data[1])
            self.assertEqual(pending.inventory.first["current_building"], "B1")

    def test_person_clears_location_and_uses_static_names(self):
        result, _ = self.service.assign(self.pending, Target("person", "Group A", borrower="Alice Current"))
        self.assertEqual(result.first["borrowed_by"], "Alice Current")
        self.assertEqual(result.first["current_group"], "Group A")
        for field in ("current_building", "current_room", "current_cabinet"):
            self.assertEqual(result.first[field], "")

    def test_one_station_assigns_across_groups_without_changing_memberships(self):
        for expected_group, expected_person in (
            ("Group A", "Alice Current"), ("Group B", "Bob Current"),
        ):
            with self.subTest(group=expected_group):
                pending = self.service.load(self.pending.code)
                target = Target("location", expected_group, "B2")
                location, _ = self.service.assign(pending, target)
                self.assertEqual(location.first["current_group"], expected_group)
                pending = self.service.load(self.pending.code)
                person, _ = self.service.assign(pending, Target("person", expected_group, borrower=expected_person))
                self.assertEqual(person.first["current_group"], expected_group)
                self.assertEqual(person.first["borrowed_by"], expected_person)
                self.assertEqual(person.first["amount"], self.item.first["amount"])
        self.assertEqual(len(self.client.writes), 4)

    def test_supplied_borrower_and_group_are_not_checked_against_user_directory(self):
        saved, _ = self.service.assign(self.pending, Target("person", "Unlisted group", borrower="Printed name"))
        self.assertEqual(saved.first["current_group"], "Unlisted group")
        self.assertEqual(saved.first["borrowed_by"], "Printed name")

    def test_every_partial_label_replaces_exactly_five_current_fields_for_both_types(self):
        fields = {
            "building": "B2", "room": "Office", "cabinet": "Desk",
            "group": "Printed group", "borrower": "Printed borrower",
        }
        for parent_kind in ("sample", "sequence_based_macromolecule_sample"):
            for label_kind in ("location", "person"):
                for field, value in fields.items():
                    with self.subTest(parent=parent_kind, label=label_kind, field=field):
                        item = bottle(kind=parent_kind)
                        item.first["borrowed_by"] = "Previous borrower"
                        item.first["owner"] = {"id": 55}
                        self.client.records[item.ref.key] = item
                        pending = Pending(copy.deepcopy(item), "Bottle", "code")
                        target = parse_target(Target(label_kind, **{field: value}).payload())
                        expected = copy.deepcopy(item.data)
                        expected[0].update(target.assignment)
                        saved, _ = self.service.assign(pending, target)
                        self.assertEqual(saved.data, expected)
                        self.assertEqual(sum(bool(saved.first[key]) for key in target.assignment), 1)

    def test_both_label_types_preserve_all_five_supplied_values(self):
        for kind in ("location", "person"):
            pending = self.service.load(self.pending.code)
            target = Target(kind, "Printed group", "Office", "101", "Desk", "Alice Printed")
            saved, _ = self.service.assign(pending, target)
            self.assertEqual(tuple(saved.first[key] for key in target.assignment),
                             ("Office", "101", "Desk", "Printed group", "Alice Printed"))

    def test_matching_assignment_skips_put(self):
        _, message = self.service.assign(self.pending, Target("location", "Group A", "B1", "204", "C3", collection_id=10))
        self.assertIn("Already", message)
        self.assertFalse(self.client.writes)

    def test_conflict_prevents_write(self):
        for field, value in (("borrowed_by", "Bob"), ("status", "Out of stock")):
            with self.subTest(field=field):
                self.client.records[self.item.ref.key] = bottle()
                self.client.records[self.item.ref.key].first[field] = value
                with self.assertRaises(Conflict):
                    self.service.assign(self.pending, Target("location", "Group A", "B2", collection_id=10))
        self.assertFalse(self.client.writes)

    def test_missing_recovery_requires_confirmation_except_audit(self):
        self.item.first["status"] = "Missing"
        pending = Pending(copy.deepcopy(self.item), "Acetone", "code")
        target = Target("location", "Group A", "B2", collection_id=10)
        with self.assertRaises(FoundConfirmation):
            self.service.assign(pending, target)
        self.assertFalse(self.client.writes)
        result, _ = self.service.assign(pending, target, audit=True)
        self.assertEqual(result.first["status"], "Available")
        self.assertTrue(result.first["important_notes"].startswith("Keep dry\nFound "))
        self.assertEqual(result.first["amount"], self.item.first["amount"])

    def test_confirmed_found_and_nontext_notes_rejection(self):
        self.item.first["status"] = "Missing"
        pending = Pending(copy.deepcopy(self.item), "Acetone", "code")
        result, _ = self.service.assign(pending, Target("person", "Group A", borrower="Alice Current"), found=True)
        self.assertEqual(result.first["status"], "Available")
        self.item.first["important_notes"] = ["not", "text"]
        self.client.records[self.item.ref.key] = self.item
        pending = Pending(copy.deepcopy(self.item), "Acetone", "code")
        with self.assertRaises(WorkflowError):
            self.service.assign(pending, Target("location", "Group A", "B2", collection_id=10), found=True)

    def test_missing_preserves_everything_else(self):
        before = copy.deepcopy(self.item.data)
        result, outcome = self.service.mark_missing(self.item)
        before[0]["status"] = "Missing"
        self.assertEqual(result.data, before)
        self.assertEqual(outcome, "missing")

    def test_authentication_error_cannot_write(self):
        self.client.inventory = Mock(side_effect=AuthenticationError("Token denied"))
        with self.assertRaises(AuthenticationError):
            self.service.assign(self.pending, Target("location", "Group A", "B2", collection_id=10))
        self.assertFalse(self.client.writes)

    def test_at_location_uses_current_not_home_and_excludes_borrower(self):
        target = Target("location", "Group A", "B1", "204", "C3", collection_id=10)
        self.assertTrue(at_location(self.item, target))
        self.item.first["borrowed_by"] = "Alice"
        self.assertFalse(at_location(self.item, target))
        target = Target("person", "Group A", "B1", "204", "C3", borrower="Alice")
        self.assertTrue(at_location(self.item, target))


if __name__ == "__main__":
    unittest.main()
