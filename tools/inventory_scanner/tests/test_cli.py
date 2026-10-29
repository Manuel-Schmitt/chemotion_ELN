import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.inventory_scanner.__main__ import main, parser
from tools.inventory_scanner.workflow import Config


class GuiLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "station.json"
        self.config.write_text(json.dumps({"base_url": "https://eln.example"}))
        self.err = io.StringIO()

    def invoke(self, args):
        with contextlib.redirect_stderr(self.err):
            return main(args)

    def test_entry_point_accepts_only_gui_launcher_options(self):
        self.assertEqual(parser().parse_args(["--config", str(self.config)]).config, self.config)
        for args in (["label", "location"], ["audit", "list"], ["scan", "--config", str(self.config)]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(args)

    def test_missing_token_is_actionable(self):
        with patch.dict(os.environ, {}, clear=True):
            status = self.invoke(["--config", str(self.config)])
        self.assertEqual(status, 1)
        self.assertIn("CHEMOTION_API_TOKEN", self.err.getvalue())

    def test_invalid_token_never_echoed(self):
        token = "PRIVATE TOKEN MUST NOT APPEAR"
        with patch.dict(os.environ, {"CHEMOTION_API_TOKEN": token}):
            status = self.invoke(["--config", str(self.config)])
        self.assertEqual(status, 1)
        self.assertNotIn(token, self.err.getvalue())

    def test_removed_audit_storage_config_has_migration_message(self):
        self.config.write_text(json.dumps({
            "base_url": "https://eln.example", "audit_directory": str(self.root / "audits"),
        }))
        with patch.dict(os.environ, {}, clear=True):
            status = self.invoke(["--config", str(self.config)])
        self.assertEqual(status, 1)
        self.assertIn("audit progress is now kept in memory", self.err.getvalue())

    def test_example_config_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "config.example.json"
        config = Config.load(path)
        self.assertTrue(config.fullscreen)
        self.assertNotIn("inventory_collection_ids", json.loads(path.read_text()))
        self.assertNotIn("group_id", json.loads(path.read_text()))
        self.assertNotIn("audit_directory", json.loads(path.read_text()))
