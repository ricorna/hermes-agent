"""Safe behavioral reproduction: extract original helpers without import-time I/O."""

import ast
import os
from pathlib import Path
import shutil
import tempfile
import unittest

ORIGINAL = Path(os.environ["JIMMY_ORIGINAL_CONTROLLER"])


class OriginalRegressions(unittest.TestCase):
    def test_no_undefined_orphan_recovery_branch(self):
        tree = ast.parse(ORIGINAL.read_text())
        branch = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Name)
            and n.test.id == "rollback_only"
        )
        wrapper = ast.parse("def isolated():\n    pass\n")
        wrapper.body[0].body = [branch]
        scope = {}
        exec(compile(ast.fix_missing_locations(wrapper), "<extracted>", "exec"), scope)
        scope["isolated"]()

    def test_stale_route_for_replacement_generation_is_rejected(self):
        tree = ast.parse(ORIGINAL.read_text())
        assertion = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Assert) and "response_sha256" in ast.unparse(n.test)
        )
        scope = {
            "v": {
                "verified": True,
                "gateway_pid": 1,
                "start_time": 1,
                "observed_at": 1,
                "response_sha256": "truthy-not-a-hash",
            },
            "st": {"pid": 1, "start_time": 999},
            "force": False,
        }
        with self.assertRaises(AssertionError):
            exec(
                compile(
                    ast.Module(body=[assertion], type_ignores=[]), "<extracted>", "exec"
                ),
                scope,
            )

    def test_restore_does_not_move_only_original(self):
        tree = ast.parse(ORIGINAL.read_text())
        helper = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "restore_plugin"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / "backup"
            backup.mkdir()
            live = root / "live"
            live.mkdir()
            (live / "model_catalog.py").write_bytes(b"original")
            (backup / "plugin-mutation-started").touch()
            scope = dict(backup=backup, live_plugin=live, shutil=shutil, os=os)
            exec(
                compile(
                    ast.Module(body=[helper], type_ignores=[]), "<extracted>", "exec"
                ),
                scope,
            )
            with self.assertRaises(FileNotFoundError):
                scope["restore_plugin"]()
            self.assertTrue(
                live.exists(), "sole original must remain live when backup is missing"
            )

    def test_approved_migration_is_accepted(self):
        tree = ast.parse(ORIGINAL.read_text())
        assertion = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Assert)
            and isinstance(n.msg, ast.Constant)
            and n.msg.value == "live config changed"
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            (cfg / "config.yaml").write_bytes(
                b"_config_version: 53\nsetting: unchanged\n"
            )
            scope = dict(
                cfg=cfg, config_before=b"_config_version: 51\nsetting: unchanged\n"
            )
            exec(
                compile(
                    ast.Module(body=[assertion], type_ignores=[]), "<extracted>", "exec"
                ),
                scope,
            )


if __name__ == "__main__":
    unittest.main()
