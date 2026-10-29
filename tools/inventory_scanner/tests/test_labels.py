import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from tools.inventory_scanner.workflow import parse_target


SCRIPT = Path(__file__).resolve().parents[2] / "generate_inventory_label.py"
SPEC = importlib.util.spec_from_file_location("generate_inventory_label", SCRIPT)
LABELS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LABELS)


class LabelGenerationTests(unittest.TestCase):
    def test_compact_payload_has_fixed_slots_and_reversible_escaping(self):
        fields = {"building": "B|1", "room": "R%2", "cabinet": "", "group": "研究組", "borrower": "Müller"}
        payload = LABELS.build_payload(fields, 10)
        self.assertEqual(payload, "ci1|B%7C1|R%252||研究組|Müller|10")
        self.assertLess(len(payload.encode("utf-8")), 65)
        self.assertEqual(len(payload.split("|")), 7)
        parsed = parse_target(payload)
        self.assertEqual((parsed.building, parsed.room, parsed.group, parsed.collection_id),
                         ("B|1", "R%2", "研究組", 10))

    def test_empty_collection_slot_and_both_categories_have_identical_wire_data(self):
        fields = {"building": "B1", "room": "204", "cabinet": "C3", "group": "Group A", "borrower": ""}
        self.assertEqual(LABELS.build_payload(fields), "ci1|B1|204|C3|Group A||")
        self.assertEqual(LABELS.build_payload(fields, 10), LABELS.build_payload(fields, 10))

    def test_invalid_text_id_and_clear_all_rejected(self):
        for value in ("\x00x", " leading", "x\n", "x" * 101, "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(LABELS.LabelError):
                LABELS.build_payload({"building": value, "room": "", "cabinet": "", "group": "", "borrower": ""})
        empty = {field: "" for field in LABELS.FIELDS}
        with self.assertRaises(LABELS.LabelError):
            LABELS.build_payload(empty)
        for identifier in (0, -1, True):
            with self.subTest(identifier=identifier), self.assertRaises(LABELS.LabelError):
                LABELS.build_payload({**empty, "borrower": "Alice"}, identifier)

    def test_svg_has_compact_caption_and_quiet_zone(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "label.svg"
            fields = {"building": "B & 1", "room": "R < 2", "cabinet": "C3",
                      "group": "Group A", "borrower": ""}
            LABELS.write_label(fields, 10, output)
            root = ET.parse(output).getroot()
            ns = {"s": "http://www.w3.org/2000/svg"}
            texts = [element.text for element in root.findall("s:text", ns)]
            self.assertIn("Building: B & 1", texts)
            self.assertIn("Room: R < 2", texts)
            self.assertIn("Cabinet: C3", texts)
            self.assertIn("Group: Group A", texts)
            self.assertIn("Collection: 10", texts)
            self.assertIn("Blank fields clear", texts)
            self.assertNotIn("Borrower:", "\n".join(texts))
            self.assertEqual(root.find("s:path", ns).attrib["transform"], "translate(1 1)")
            self.assertEqual(root.find("s:rect", ns).attrib["fill"], "white")
            with self.assertRaises(FileExistsError):
                LABELS.write_label(fields, 10, output)

    def test_cli_runs_standalone_without_station_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "label.svg"
            result = subprocess.run([
                sys.executable, str(SCRIPT), "location", "--building", "B1",
                "--output", str(output),
            ], check=True, capture_output=True, text=True)
            self.assertIn("Created", result.stdout)
            self.assertTrue(output.exists())

    @unittest.skipUnless(shutil.which("rsvg-convert") and shutil.which("dmtxread"),
                         "Optional Data Matrix round-trip test requires rsvg-convert and dmtxread")
    def test_auto_encoded_svg_decodes_exact_payload_for_ascii_unicode_and_escapes(self):
        targets = [
            ({"building": "B1", "room": "204", "cabinet": "C3", "group": "Working Group A", "borrower": ""}, 100),
            ({"building": "Gebäude", "room": "R|2", "cabinet": "", "group": "研究組", "borrower": "Müller 🧪"}, None),
            ({"building": "B" * 100, "room": "R" * 100, "cabinet": "C" * 100,
              "group": "Group A", "borrower": "Alice"}, 10),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for index, (fields, collection_id) in enumerate(targets):
                with self.subTest(index=index):
                    svg = Path(directory) / f"{index}.svg"
                    png = Path(directory) / f"{index}.png"
                    LABELS.write_label(fields, collection_id, svg)
                    if index == 2:
                        root = ET.parse(svg).getroot()
                        self.assertGreater(float(root.attrib["viewBox"].split()[2]), 200)
                    subprocess.run(["rsvg-convert", "-w", "1200", "-o", str(png), str(svg)],
                                   check=True, capture_output=True)
                    decoded = subprocess.run(["dmtxread", "-n", str(png)],
                                             check=True, capture_output=True)
                    self.assertEqual(decoded.stdout.rstrip(b"\n"),
                                     LABELS.build_payload(fields, collection_id).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
