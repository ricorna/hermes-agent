"""Offline controller contracts; unittest intentionally avoids project conftest."""

import copy
import fcntl
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/maintenance"
sys.path.insert(0, str(SCRIPTS))
import jimmy_v0231_activate as c
import jimmy_v0231_watchdog as w


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "activate-test"
        self.backup = self.work / "backup"
        self.backup.mkdir(parents=True)
        self.nonce = "a" * 64
        self.prepared = {
            "schema": c.SCHEMA,
            "attempt": self.work.name,
            "nonce": self.nonce,
            "activation_unit": "owned.service",
        }
        self.save_owner()

    def save_owner(self):
        c.write_json(self.backup / "prepared.json", self.prepared)
        c.write_json(
            self.root / "attempt-owner.json",
            {
                **self.prepared,
                "prepared_sha256": c.digest(
                    (self.backup / "prepared.json").read_bytes()
                ),
            },
        )

    def journal(self, initial):
        state = copy.deepcopy(initial)
        journal = c.Journal(self.root, self.work, lambda: copy.deepcopy(state))
        journal.allow("initial", [state])
        return journal, state


class OwnershipTests(Workspace):
    def test_import_without_production_reads(self):
        for name in ("jimmy_v0231_activate", "jimmy_v0231_watchdog"):
            spec = importlib.util.spec_from_file_location(
                "isolated_" + name, SCRIPTS / (name + ".py")
            )
            module = importlib.util.module_from_spec(spec)
            with mock.patch.object(
                Path, "read_text", side_effect=AssertionError("import I/O")
            ):
                spec.loader.exec_module(module)

    def test_deployed_hyphenated_pair_imports_without_canonical_alias(self):
        import shutil

        shutil.copyfile(
            SCRIPTS / "jimmy_v0231_activate.py", self.root / "jimmy-activate.py"
        )
        shutil.copyfile(
            SCRIPTS / "jimmy_v0231_watchdog.py", self.root / "jimmy-watchdog.py"
        )
        probe = "import runpy; m=runpy.run_path('jimmy-watchdog.py'); print(m['controller'].__file__)"
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("jimmy-activate.py", result.stdout)

    def test_systemd_252_omits_empty_command_arrays_even_with_all(self):
        empty_arrays = {
            "ExecStartPre",
            "ExecStartPost",
            "ExecCondition",
            "ExecReload",
            "ExecStop",
            "ExecStopPost",
            "EnvironmentFiles",
        }
        props = {key: "" for key in c.UNIT_PROPERTIES if key not in empty_arrays}
        fragment = self.root / "owned.service"
        fragment.write_text("[Service]\nExecStart=/usr/bin/true\n")
        props["FragmentPath"] = str(fragment)
        props["ExecStart"] = (
            "{ path=/usr/bin/true ; argv[]=/usr/bin/true ; ignore_errors=no ; }"
        )
        result = c.unit_state(
            lambda args: "\n".join(f"{k}={v}" for k, v in props.items()),
            "owned.service",
        )
        self.assertEqual(result["loaded"]["ExecStartPre"], [])
        del props["KillMode"]
        with self.assertRaises(AssertionError):
            c.unit_state(
                lambda args: "\n".join(f"{k}={v}" for k, v in props.items()),
                "owned.service",
            )

    def test_foreign_stale_legacy_and_tampered_prepared(self):
        journal, _state = self.journal({"launcher": "original"})
        for field, value in [("nonce", "b" * 64), ("attempt", "other"), ("schema", 1)]:
            with self.subTest(field=field):
                self.save_owner()
                owner = c.read_json(self.root / "attempt-owner.json")
                owner[field] = value
                c.write_json(self.root / "attempt-owner.json", owner)
                with self.assertRaises(AssertionError):
                    journal.check()
        self.save_owner()
        c.write_json(
            self.backup / "prepared.json", {**self.prepared, "platforms": "foreign"}
        )
        with self.assertRaises(AssertionError):
            journal.check()

    def test_every_changed_artifact_refuses_before_action(self):
        initial = {
            "plugin": "old",
            "launcher": "old",
            "unit": {"physical": "old", "loaded": "old"},
            "gateway": {"pid": 12, "start_time": 99, "code_sha": c.OLD_SHA},
        }
        for key in initial:
            with self.subTest(key=key):
                journal, state = self.journal(initial)
                state[key] = "foreign"
                action = mock.Mock()
                with self.assertRaises(AssertionError):
                    journal.change("stop", initial, action)
                action.assert_not_called()

    def test_interruption_on_both_sides_of_each_boundary(self):
        for phase in (
            "drop-write",
            "reload-add",
            "stop",
            "reload-remove",
            "launcher-write",
        ):
            for after in (False, True):
                with self.subTest(phase=phase, after=after):
                    journal, state = self.journal({"generation": "old"})

                    def action():
                        if after:
                            state["generation"] = "new"
                        raise InterruptedError()

                    with self.assertRaises(InterruptedError):
                        journal.change(phase, {"generation": "new"}, action)
                    self.assertEqual(journal.check(), state)
                    journal.change(
                        "retry",
                        {"generation": "new"},
                        lambda: state.update(generation="new"),
                    )
                    self.assertEqual(journal.check(), {"generation": "new"})

    def test_exactly_owned_stopped_generation_is_recoverable(self):
        journal, state = self.journal({"gateway": {"pid": 10, "start_time": 20}})
        journal.change("stop", {"gateway": None}, lambda: state.update(gateway=None))
        self.assertEqual(journal.check(), {"gateway": None})
        state["gateway"] = {"pid": 10, "start_time": 21}
        with self.assertRaises(AssertionError):
            journal.check()


class PluginTests(Workspace):
    def fixture(self):
        live = self.root / "live"
        candidate = self.root / "candidate"
        live.mkdir()
        candidate.mkdir()
        (live / "model_catalog.py").write_bytes(b"original modified catalog\x00")
        (live / ".hermes-catalog.json").write_bytes(b"untracked catalog")
        (candidate / "model_catalog.py").write_bytes(b"candidate")
        old, new = c.tree_identity(live), c.tree_identity(candidate)

        def snapshot():
            return {
                "plugin": c.tree_identity(live),
                "original_plugin": c.tree_identity(self.backup / "original-plugin"),
                "rejected_plugin": c.tree_identity(self.backup / "rejected-plugin"),
            }

        journal = c.Journal(self.root, self.work, snapshot)
        journal.allow("initial", [snapshot()])

        def change(phase, updates, action):
            expected = journal.check()
            expected.update(updates)
            journal.change(phase, expected, action)

        return live, candidate, old, new, change, journal

    def test_all_install_and_restore_rename_interruptions(self):
        for phase in (
            "plugin-save-original",
            "plugin-install",
            "plugin-reject",
            "plugin-restore",
        ):
            for after in (False, True):
                with self.subTest(phase=phase, after=after):
                    # Fresh owned workspace for every crash point.
                    with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                        saved_root, saved_backup, saved_work = (
                            self.root,
                            self.backup,
                            self.work,
                        )
                        self.root = Path(tmp)
                        self.work = self.root / "activate-test"
                        self.backup = self.work / "backup"
                        self.backup.mkdir(parents=True)
                        self.save_owner()
                        live, candidate, old, new, change, journal = self.fixture()
                        fired = []

                        def interrupt(name, updates, action):
                            def wrapped():
                                if name == phase and not fired:
                                    fired.append(True)
                                    if after:
                                        action()
                                    raise InterruptedError(name)
                                action()

                            change(name, updates, wrapped)

                        try:
                            c.plugin_install(
                                live, self.backup, candidate, old, new, interrupt
                            )
                            c.plugin_restore(live, self.backup, old, new, interrupt)
                        except InterruptedError:
                            pass
                        self.assertTrue(fired)
                        journal.check()
                        c.plugin_restore(live, self.backup, old, new, change)
                        c.plugin_restore(live, self.backup, old, new, change)
                        self.assertEqual(c.tree_identity(live), old)
                        self.assertEqual(
                            (live / ".hermes-catalog.json").read_bytes(),
                            b"untracked catalog",
                        )
                        self.root, self.backup, self.work = (
                            saved_root,
                            saved_backup,
                            saved_work,
                        )

    def test_missing_or_corrupt_backup_never_moves_live(self):
        live, candidate, old, new, change, _ = self.fixture()
        c.plugin_install(live, self.backup, candidate, old, new, change)
        original = self.backup / "original-plugin"
        (original / "model_catalog.py").write_bytes(b"corrupt")
        for missing in (False, True):
            if missing:
                original.rename(self.backup / "hidden-original")
            with self.assertRaises(AssertionError):
                c.plugin_restore(live, self.backup, old, new, change)
            self.assertEqual(c.tree_identity(live), new)
            self.assertFalse((self.backup / "rejected-plugin").exists())

    def test_only_generated_python_caches_are_ignored(self):
        live, _, old, _, _, _ = self.fixture()
        cache = live / "__pycache__"
        cache.mkdir()
        (cache / "x.pyc").write_bytes(b"cache")
        self.assertEqual(c.tree_identity(live), old)
        (live / ".hermes-catalog.json").write_bytes(b"changed")
        self.assertNotEqual(c.tree_identity(live), old)

    def test_partial_candidate_staging_leaves_original_untouched(self):
        live, candidate, old, new, change, _ = self.fixture()
        (self.backup / "candidate-plugin").mkdir()
        with self.assertRaises(AssertionError):
            c.plugin_install(live, self.backup, candidate, old, new, change)
        self.assertEqual(c.tree_identity(live), old)


class ConfigRouteLauncherTests(Workspace):
    def test_migration_only_allows_reviewed_version_change(self):
        original = {
            "_config_version": 51,
            "settings": {"model": "keep", "enabled": True},
        }
        expected = {**original, "_config_version": 53}
        c.validate_migration(original, expected, copy.deepcopy(expected))
        for side in ("expected", "actual"):
            changed = copy.deepcopy(expected)
            changed["settings"]["model"] = "changed"
            with self.assertRaises(AssertionError):
                c.validate_migration(
                    original,
                    changed if side == "expected" else expected,
                    changed if side == "actual" else expected,
                )

    def test_route_generation_binding_and_freshness(self):
        challenge = {
            "attempt": "activate-test",
            "nonce": self.nonce,
            "token": "random",
            "task_id": "task",
            "context_id": "context",
            "pid": 123,
            "start_time": 456,
            "code_sha": c.SHA,
            "requested_at": 1000,
        }
        verdict = {
            **challenge,
            "accepted": True,
            "observed_at": 1010,
            "reply_sha256": "f" * 64,
        }
        c.validate_route(challenge, verdict, challenge, now=1020)
        for key in challenge:
            changed = {**verdict, key: "wrong"}
            with self.subTest(key=key), self.assertRaises(AssertionError):
                c.validate_route(challenge, changed, challenge, now=1020)
        for key in ("pid", "start_time", "code_sha"):
            with self.subTest(live=key), self.assertRaises(AssertionError):
                c.validate_route(
                    challenge, verdict, {**challenge, key: "replacement"}, now=1020
                )
        for changes in (
            {"observed_at": 999},
            {"observed_at": 1021},
            {"reply_sha256": "truthy"},
        ):
            with self.assertRaises(AssertionError):
                c.validate_route(challenge, {**verdict, **changes}, challenge, now=1020)
        # Launcher verification/status query consumed the remaining TTL.
        with self.assertRaises(AssertionError):
            c.validate_route(challenge, verdict, challenge, now=1121)
        with self.assertRaises(AssertionError):
            c.validate_route(
                {**challenge, "task_id": ""},
                {**verdict, "task_id": ""},
                challenge,
                now=1020,
            )

    def test_regular_symlink_dangling_and_absent_launcher_restore(self):
        launcher = self.root / "hermes"
        for kind in ("regular", "symlink", "dangling", "absent"):
            with self.subTest(kind=kind):
                launcher.unlink(missing_ok=True)
                target = self.root / ("target" if kind != "dangling" else "missing")
                if kind == "regular":
                    launcher.write_bytes(b"original")
                    launcher.chmod(0o751)
                elif kind in ("symlink", "dangling"):
                    if kind == "symlink":
                        target.touch()
                    launcher.symlink_to(target)
                identity = c.file_identity(launcher)
                metadata = {
                    "exists": launcher.exists() or launcher.is_symlink(),
                    "symlink": launcher.is_symlink(),
                    "target": str(target),
                    "mode": 0o751,
                }
                c.write_json(self.backup / "launcher.json", metadata)
                (self.backup / "launcher").write_bytes(b"original")
                c.atomic(launcher, b"candidate")
                c.restore_launcher(launcher, self.backup, identity)
                c.restore_launcher(launcher, self.backup, identity)
                self.assertEqual(c.file_identity(launcher), identity)


class DeadlineTests(Workspace):
    def test_lock_contention_expires_without_blocking(self):
        path = self.root / "lock"
        with path.open("w") as locked:
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(TimeoutError):
                with c.attempt_lock(path, c.Deadline(0.05)):
                    self.fail("lock unexpectedly acquired")
        with c.attempt_lock(path, c.Deadline(2)):
            pass

    def test_global_deadline_does_not_reset_between_local_budgets(self):
        with mock.patch.object(c.time, "monotonic", side_effect=[100, 105, 109, 111]):
            deadline = c.Deadline(10)
            self.assertEqual(deadline.remaining(600), 5)
            self.assertEqual(deadline.remaining(600), 1)
            with self.assertRaises(TimeoutError):
                deadline.remaining(600)

    def test_sqlite_backup_and_expired_backup(self):
        source = self.root / "source.db"
        dest = self.root / "dest.db"
        with sqlite3.connect(source) as db:
            db.execute("create table evidence(value)")
            db.execute("insert into evidence values ('preserved')")
        c.backup_sqlite(source, dest, c.Deadline(3))
        with sqlite3.connect(dest) as db:
            self.assertEqual(
                db.execute("select value from evidence").fetchone(), ("preserved",)
            )
        with self.assertRaises(TimeoutError):
            c.backup_sqlite(source, dest, c.Deadline(-1))

    def test_sqlite_busy_backup_obeys_global_deadline(self):
        source = self.root / "busy.db"
        with sqlite3.connect(source) as writer:
            writer.execute("create table evidence(value)")
            writer.commit()
            writer.execute("begin exclusive")
            writer.execute("insert into evidence values ('uncommitted')")
            with self.assertRaises(TimeoutError):
                c.backup_sqlite(source, self.root / "busy-backup.db", c.Deadline(0.05))
            writer.rollback()

    def test_expired_command_never_spawns(self):
        with mock.patch.object(c.subprocess, "Popen") as spawn:
            with self.assertRaises(TimeoutError):
                c.command(["never"], c.Deadline(-1))
            spawn.assert_not_called()

    def test_command_timeout_terminates_child_group_before_return(self):
        pidfile = self.root / "child.pid"
        child_lock = self.root / "child.lock"
        program = "\n".join(
            [
                "import os,signal,pathlib,fcntl",
                "pid=os.fork()",
                "if pid == 0:",
                "    lock=open(" + repr(str(child_lock)) + ', "w")',
                "    fcntl.flock(lock, fcntl.LOCK_EX)",
                "    pathlib.Path("
                + repr(str(pidfile))
                + ").write_text(str(os.getpid()))",
                "signal.pause()",
            ]
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            c.command([sys.executable, "-c", program], c.Deadline(5), timeout=1)
        self.assertGreater(int(pidfile.read_text()), 0)
        # The child holds this kernel lock until killed; no process-list privilege needed.
        with child_lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


class WatchdogTests(Workspace):
    def prepare_worker(self):
        identity = {
            "unit": {"loaded": {"ControlGroup": "/owned"}},
            "invocation": "b" * 32,
        }
        self.prepared.update(worker_identity=identity, worker_pid=123, worker_start=456)
        self.save_owner()
        commands = []

        def cmd(args, timeout=180):
            commands.append(args)
            if "MainPID" in args:
                return "0" if any("stop" in row for row in commands) else "123"
            return ""

        return identity, commands, cmd

    def test_foreign_target_never_stopped(self):
        _identity, commands, cmd = self.prepare_worker()
        with mock.patch.object(w, "worker_identity", return_value={"foreign": True}):
            with self.assertRaises(AssertionError):
                w.fence_worker(
                    self.root, self.work, "owned.service", cmd, c.Deadline(3)
                )
        self.assertFalse(any("stop" in row for row in commands))

    def test_stopped_worker_preserves_all_foreign_and_cgroup_fences(self):
        identity, commands, cmd = self.prepare_worker()
        commands.append(["stop"])
        stopped = copy.deepcopy(identity)
        stopped["unit"]["loaded"]["ControlGroup"] = ""
        for change in ("invocation", "static", "group", "descendants"):
            actual = copy.deepcopy(stopped)
            if change == "invocation":
                actual["invocation"] = "c" * 32
            elif change == "static":
                actual["unit"]["physical"] = {"foreign": True}
            elif change == "group":
                actual["unit"]["loaded"]["ControlGroup"] = "/foreign"
            commands.clear()
            commands.append(["stop"])
            with (
                mock.patch.object(w, "worker_identity", return_value=actual),
                mock.patch.object(
                    c, "cgroup_empty", return_value=change != "descendants"
                ),
            ):
                with self.subTest(change=change), self.assertRaises(AssertionError):
                    w.fence_worker(
                        self.root, self.work, "owned.service", cmd, c.Deadline(3)
                    )
            self.assertEqual(sum("stop" in row for row in commands), 1)

    def test_descendants_must_be_empty_before_recovery(self):
        identity, commands, cmd = self.prepare_worker()
        with (
            mock.patch.object(w, "worker_identity", return_value=identity),
            mock.patch.object(c, "proc_start", return_value=456),
            mock.patch.object(c, "cgroup_empty", return_value=False),
        ):
            with self.assertRaises(AssertionError):
                w.supervise(
                    self.root,
                    self.work,
                    "owned.service",
                    cmd,
                    c.Deadline(10),
                    "recovery.py",
                )
        self.assertTrue(any("stop" in row for row in commands))
        self.assertFalse(any("recovery.py" in row for row in commands))

    def test_commit_survives_disarm_failure_and_watchdog_never_stops(self):
        _, commands, cmd = self.prepare_worker()
        receipt = {"nonce": self.nonce, "attempt": self.work.name}
        c.commit_receipt(self.root, self.work, receipt)
        with self.assertRaises(RuntimeError):
            raise RuntimeError("timer disarm failed")
        self.assertFalse(
            w.fence_worker(self.root, self.work, "owned.service", cmd, c.Deadline(3))
        )
        self.assertEqual(commands, [])
        owner = c.read_json(self.root / "attempt-owner.json")
        owner["nonce"] = "c" * 64
        c.write_json(self.root / "attempt-owner.json", owner)
        with self.assertRaises(AssertionError):
            w.fence_worker(self.root, self.work, "owned.service", cmd, c.Deadline(3))

    def test_commit_and_recovery_are_mutually_exclusive(self):
        c.claim_decision(self.work, self.nonce, "recover")
        with self.assertRaises(AssertionError):
            c.commit_receipt(
                self.root, self.work, {"nonce": self.nonce, "attempt": self.work.name}
            )
        self.assertFalse((self.work / "activated.json").exists())

    def test_commit_reservation_without_receipt_fails_closed(self):
        _, commands, cmd = self.prepare_worker()
        c.claim_decision(self.work, self.nonce, "commit")
        with self.assertRaises(AssertionError):
            w.fence_worker(self.root, self.work, "owned.service", cmd, c.Deadline(3))
        self.assertEqual(commands, [])


class ControllerFlowTests(Workspace):
    def controller(self):
        controller = object.__new__(c.Controller)
        controller.work = self.work
        controller.backup = self.backup
        controller.budget = c.Deadline(10)
        controller.launcher = self.root / "launcher"
        controller.attempt = self.work.name
        controller.task_id = "task"
        controller.context_id = "context"
        controller.before = {"pid": 1, "platforms": {}}
        controller.d = {
            "attempt": self.work.name,
            "nonce": self.nonce,
            "route_token": "unique",
            "steps": {},
        }
        controller.disarm = mock.Mock(side_effect=RuntimeError("disarm failed"))
        controller.save = mock.Mock()
        return controller

    def test_actual_route_acceptance_rechecks_after_launcher_and_commit_is_final(self):
        for result in ("accepted", "replacement", "expired"):
            with self.subTest(result=result):
                (self.work / "activated.json").unlink(missing_ok=True)
                (self.work / "terminal-decision").unlink(missing_ok=True)
                controller = self.controller()
                clock = [1000]
                pid = [123]
                generation = {"pid": 123, "start_time": 456, "code_sha": c.SHA}
                state = {
                    "gateway": generation,
                    "launcher": c.file_identity(controller.launcher),
                }
                controller.guard = c.Journal(
                    self.root, self.work, lambda: copy.deepcopy(state)
                )
                controller.guard.allow("candidate", [state])

                def status(verb):
                    return {
                        "pid": pid[0],
                        "start_time": 456,
                        "code_sha": c.SHA,
                        "answered_at": clock[0],
                        "active_agents": 0,
                        "gateway_state": "running",
                    }

                controller.ctl = status
                controller.ready = lambda *args: (status("status"), {})

                def change(phase, updates, action):
                    action()
                    state.update(updates)
                    controller.guard.allow(phase, [state])

                controller.change = change

                def verify(*args):
                    if result == "replacement":
                        pid[0] = 124
                    if result == "expired":
                        clock[0] = 1121
                    return "version verified"

                controller.run_command = verify
                original_write = c.write_json

                def write(path, value):
                    original_write(path, value)
                    if path.name == "awaiting-route.json":
                        original_write(
                            self.work / "route-verdict.json",
                            {
                                **value,
                                "accepted": True,
                                "observed_at": clock[0],
                                "reply_sha256": "f" * 64,
                            },
                        )

                with (
                    mock.patch.object(c, "ROOT", self.root),
                    mock.patch.object(c, "proc_start", return_value=456),
                    mock.patch.object(c.time, "time", side_effect=lambda: clock[0]),
                    mock.patch.object(c, "write_json", side_effect=write),
                ):
                    if result == "accepted":
                        self.assertEqual(controller.accept_route(), 0)
                        self.assertTrue(c.committed_owned(self.root, self.work))
                        controller.disarm.assert_called_once()
                    else:
                        with self.assertRaises(AssertionError):
                            controller.accept_route()
                        self.assertFalse((self.work / "activated.json").exists())
                        controller.disarm.assert_not_called()

    def test_existing_attempt_refused_without_overwriting_evidence(self):
        controller = self.controller()
        controller.rollback_only = False
        controller.drop = self.root / "drop"
        controller.committed = self.work / "activated.json"
        with mock.patch.object(c, "ROOT", self.root), self.assertRaises(AssertionError):
            controller.run()
        controller.save.assert_not_called()

    def test_run_command_is_the_phase_logger_used_by_activation(self):
        controller = self.controller()
        controller.cmd = mock.Mock(return_value="safe result")
        self.assertEqual(controller.run_command("check", ["safe"]), "safe result")
        self.assertEqual((self.work / "check.log").read_text(), "safe result")
        self.assertEqual(controller.d["steps"]["check"], 0)

    def test_expiry_during_snapshot_prevents_mutation(self):
        remaining = iter([1, 1, -1])

        def time_check():
            if next(remaining) <= 0:
                raise TimeoutError("expired during snapshot")

        journal = c.Journal(self.root, self.work, lambda: {"state": "old"})
        journal.allow("initial", [{"state": "old"}])
        journal.time_check = time_check
        action = mock.Mock()
        with self.assertRaises(TimeoutError):
            journal.change("change", {"state": "new"}, action)
        action.assert_not_called()

    def test_actual_stopped_snapshot_requires_empty_owned_cgroup(self):
        controller = self.controller()
        controller.unit = "owned.service"
        controller.drop = self.root / "drop.conf"
        controller.live_plugin = self.root / "plugin"
        controller.original_cgroup = "/owned"
        controller.cmd = lambda args: "0" if "MainPID" in args else "/owned"
        controller.ctl = mock.Mock(
            side_effect=AssertionError("stopped process has no status")
        )
        baseline = {"loaded": {"ControlGroup": "/owned"}, "physical": {}}
        with (
            mock.patch.object(
                c, "unit_state", side_effect=lambda *args: copy.deepcopy(baseline)
            ),
            mock.patch.object(c, "cgroup_empty", return_value=True) as empty,
        ):
            state = controller.snapshot()
            self.assertIsNone(state["gateway"])
            empty.assert_called_once_with("/owned")
        with (
            mock.patch.object(
                c, "unit_state", side_effect=lambda *args: copy.deepcopy(baseline)
            ),
            mock.patch.object(c, "cgroup_empty", return_value=False),
            self.assertRaises(AssertionError),
        ):
            controller.snapshot()
        controller.ctl.assert_not_called()

    def test_recovery_deadline_not_reset_on_retry(self):
        controller = self.controller()
        controller.recovery_started = False
        controller.guard = mock.Mock()
        controller.guard.check.side_effect = RuntimeError("held")
        self.prepared["gateway_cgroup"] = "/owned"
        self.save_owner()
        with mock.patch.object(c, "ROOT", self.root):
            with self.assertRaises(RuntimeError):
                controller.rollback()
            first = controller.budget
            with self.assertRaises(RuntimeError):
                controller.rollback()
            self.assertIs(controller.budget, first)

    def test_corrupt_launcher_backup_refuses_before_replacing_live(self):
        launcher = self.root / "launcher"
        launcher.write_bytes(b"candidate")
        c.write_json(
            self.backup / "launcher.json",
            {"exists": True, "symlink": False, "mode": 0o755},
        )
        (self.backup / "launcher").write_bytes(b"corrupt")
        expected = {"kind": "file", "sha256": c.digest(b"original"), "mode": 0o755}
        with self.assertRaises(AssertionError):
            c.restore_launcher(launcher, self.backup, expected)
        self.assertEqual(launcher.read_bytes(), b"candidate")


if __name__ == "__main__":
    unittest.main()
