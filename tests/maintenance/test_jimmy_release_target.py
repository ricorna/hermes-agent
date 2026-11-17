"""Frozen v0.23.2 target and preflight source fence, with no production I/O."""

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts/maintenance"
sys.path.insert(0, str(SCRIPTS))
import jimmy_v0231_activate as c

ROOT = pathlib.Path("/home/jimmy/.local/state/hermes-fleet/jimmy-v0232-reconcile-t_1e63040a")
SHA = "769c26ccd1b1f9d07a800c1a853dbcd8fb5f67e7"
COMPOSITION_SHA = "e850d48d9ac707f01c619d1d4ddd9afcc9eeaace"
BASE = "ae2a81ccc00e9b7c06301523e5bb09608397149c"
PLUGIN = "32e4000c6e58133f7877309f8e79db8b12d1e1f3"


class SourceFencePassed(Exception):
    """Stop before plugin, production, service or configuration preflight."""


class ReleaseTargetTests(unittest.TestCase):
    def test_frozen_release_and_plugin_bindings(self):
        self.assertEqual(c.ROOT, ROOT)
        self.assertEqual(c.SRC, ROOT / "source")
        self.assertEqual(c.REVIEWED_PLUGIN, ROOT / "reviewed-plugin")
        self.assertEqual(c.VENV, ROOT / "source/.venv")
        self.assertEqual((c.SHA, c.BASE, c.PLUGIN_SHA), (SHA, BASE, PLUGIN))
        self.assertEqual(c.OLD_SHA, "f924a08357f95c24f826d595c48bb8da06c228e2")
        self.assertEqual(c.OLD_PLUGIN_SHA, "c92c27c9f919178a58974a72333b473c6cb2e71d")

    def source_fence(self, head=SHA, release=BASE, dirty="", composition=COMPOSITION_SHA):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            validation = {"status": "validated_reviewed_candidate_not_activated", "steps": {
                name: {"rc": 0} for name in ("state-suite", "connect-vault-suite", "directsdk-offline", "packaging")
            }}
            (root / "reviewed-validation.json").write_text(json.dumps(validation), encoding="utf-8")
            ctl = object.__new__(c.Controller)
            ctl.work = root / "attempt"
            ctl.save = lambda: None

            def command(args):
                if args[2] == c.REVIEWED_PLUGIN:
                    raise SourceFencePassed
                self.assertEqual(args[:3], ["git", "-C", c.SRC])
                return {
                    ("rev-parse", "HEAD"): head,
                    ("rev-parse", "HEAD^"): composition,
                    ("rev-parse", "HEAD^^2"): release,
                    ("status", "--porcelain"): dirty,
                }[tuple(args[3:])]

            ctl.cmd = command
            with mock.patch.object(c, "ROOT", root):
                ctl.preflight()

    def test_accepts_exact_clean_combined_head_and_release_parent(self):
        with self.assertRaises(SourceFencePassed):
            self.source_fence()

    def test_rejects_wrong_combined_head(self):
        with self.assertRaises(AssertionError):
            self.source_fence(head="0" * 40)

    def test_rejects_wrong_composition_parent(self):
        with self.assertRaises(AssertionError):
            self.source_fence(composition="0" * 40)

    def test_rejects_wrong_release_parent(self):
        with self.assertRaises(AssertionError):
            self.source_fence(release="0" * 40)

    def test_rejects_dirty_candidate(self):
        with self.assertRaises(AssertionError):
            self.source_fence(dirty=" M run_agent.py")


if __name__ == "__main__":
    unittest.main()
