"""Jimmy-only v0.23.2 cutover with independent timer watchdog and bounded rollback."""

import os, pathlib, subprocess, json, time, fcntl, socket, sys, shutil, sqlite3, hashlib, re
import contextlib, copy, secrets, signal, shlex, argparse
import logging

LOG = logging.getLogger(__name__)
ROOT = pathlib.Path("/home/jimmy/.local/state/hermes-fleet/jimmy-v0232-reconcile-t_1e63040a")
SRC = ROOT / "source"
VENV = SRC / ".venv"
REVIEWED_PLUGIN = ROOT / "reviewed-plugin"
OLD = ROOT.with_name("jimmy-stable-v0225-20261031") / "source"
OLD_SHA = "f924a08357f95c24f826d595c48bb8da06c228e2"
BASE = "ae2a81ccc00e9b7c06301523e5bb09608397149c"
SHA = "769c26ccd1b1f9d07a800c1a853dbcd8fb5f67e7"
COMPOSITION_SHA = "e850d48d9ac707f01c619d1d4ddd9afcc9eeaace"
PLUGIN_SHA = "32e4000c6e58133f7877309f8e79db8b12d1e1f3"
OLD_PLUGIN_SHA = "c92c27c9f919178a58974a72333b473c6cb2e71d"
TOKEN = "JIMMY_V0231_ROUTE_7F3C_OK"
DBS = [
    "state.db",
    "kanban.db",
    "verification_evidence.db",
    "cron/executions.db",
    "cron/deliveries.db",
    "kanban/kanban.db",
]


def atomic(p, b, mode=384):
    p = pathlib.Path(p)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("wb") as f:
        os.fchmod(f.fileno(), mode)
        f.write(b)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    fd = os.open(p.parent, os.O_RDONLY)
    os.fsync(fd)
    os.close(fd)


def durable_rename(source, destination):
    os.replace(source, destination)
    for parent in {pathlib.Path(source).parent, pathlib.Path(destination).parent}:
        fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def quoted(s):
    assert "\n" not in s and "\x00" not in s
    return (
        '"'
        + s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
        + '"'
    )


def restore_dbs(cfg, backup, deadline):
    manifest = json.loads((backup / "db-manifest.json").read_text(encoding="utf-8-sig"))
    failed = backup / "failed-state"
    failed.mkdir(exist_ok=True)
    for n, present in manifest.items():
        deadline.remaining()
        dest = cfg / n
        for suffix in ("", "-wal", "-shm"):
            p = pathlib.Path(str(dest) + suffix)
            if p.exists():
                rejected = failed / (n.replace("/", "-") + suffix)
                if rejected.exists():
                    p.unlink()
                else:
                    durable_rename(p, rejected)
        if present:
            source = backup / n.replace("/", "-")
            with (
                source.open("rb") as reader,
                dest.with_name(dest.name + ".restore-tmp").open("wb") as writer,
            ):
                while chunk := reader.read(1024 * 1024):
                    deadline.remaining()
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            deadline.remaining()
            durable_rename(dest.with_name(dest.name + ".restore-tmp"), dest)


class Controller:
    def __init__(
        self, attempt, budget, rollback_only=False, task_id=None, context_id=None
    ):
        self.attempt = attempt
        self.budget = budget
        self.rollback_only = rollback_only
        self.task_id = task_id
        self.context_id = context_id
        OLD_FACTS = json.loads(
            next((OLD.parent / "stage-home/installs").glob("*/facts.json")).read_text(
                encoding="utf-8-sig"
            )
        )
        self.old_python = (
            pathlib.Path(OLD_FACTS["packages"]["venv"]["environment"]) / "bin/python"
        )
        self.old_store_python = next(
            (OLD.parent / "stage-home/tools").glob("python-*/bin/python3")
        )
        assert re.fullmatch("(?:rollback|activate)[a-z0-9-]{1,24}", self.attempt)
        assert not os.getenv("_HERMES_GATEWAY"), "gateway environment prohibited"
        self.h = pathlib.Path("/home/jimmy")
        assert self.h == pathlib.Path.home() and socket.gethostname() == "jimmy"
        self.cfg = self.h / ".hermes"
        self.unit = "hermes-gateway.service"
        self.work = ROOT / "activation-attempts" / self.attempt
        self.backup = self.work / "backup"
        self.drop = (
            self.h
            / ".config/systemd/user/hermes-gateway.service.d/zz-jimmy-t-1e63040a.conf"
        )
        self.launcher = self.h / ".local/bin/hermes"
        self.timer = "hermes-jimmy-rollback-t-1e63040a-" + self.attempt
        self.committed = self.work / "activated.json"
        self.rolled = self.work / "rollback-performed.json"
        self.activation_unit = (
            "hermes-jimmy-activate-t-1e63040a-" + self.attempt + ".service"
        )
        self.live_plugin = (
            self.cfg / "plugins/claude-subscription-directsdk-experimental"
        )
        self.env = {
            "HOME": str(self.h),
            "USER": "jimmy",
            "LOGNAME": "jimmy",
            "LANG": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": "/run/user/" + str(unix_uid()),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/"
            + str(unix_uid())
            + "/bus",
            "HERMES_HOME": str(self.cfg),
        }
        self.principal = "jimmy-t-1e63040a-" + self.attempt
        self.guard = Journal(
            ROOT, self.work, self.snapshot, lambda: self.budget.remaining()
        )
        self.recovery_started = rollback_only
        self.original_cgroup = ""
        self.prepared = None

    def cmd(self, args, timeout=300, cwd=SRC):
        return command(
            args,
            self.budget,
            timeout,
            cwd=cwd,
            env={**self.env, "GIT_OPTIONAL_LOCKS": "0"},
        )

    def ctl(self, verb):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(self.budget.remaining(10))
            s.connect(str(self.cfg / "gateway.sock"))
            s.sendall(json.dumps({"verb": verb}).encode() + b"\n")
            b = b""
            while b"\n" not in b:
                s.settimeout(self.budget.remaining(10))
                x = s.recv(65536)
                if not x:
                    raise RuntimeError("empty control response")
                b += x
            a = json.loads(b)
            assert a["ok"]
            return a["result"]

    def disarm(self):
        self.cmd(["systemctl", "--user", "stop", self.timer + ".timer"])

    def ready(self, sha, oldpid=None, platforms=None):
        deadline = time.monotonic() + self.budget.remaining(240)
        while time.monotonic() < deadline:
            try:
                st = self.ctl("status")
                ident = self.ctl("identify")
                pid = int(
                    self.cmd(
                        [
                            "systemctl",
                            "--user",
                            "show",
                            self.unit,
                            "-p",
                            "MainPID",
                            "--value",
                        ]
                    ).strip()
                )
                connected = all(
                    (
                        st.get("platforms", {}).get(k, {}).get("state") == "connected"
                        and st["platforms"][k].get("writer_pid") == pid
                        and (
                            st["platforms"][k].get("writer_start_time")
                            == st["start_time"]
                        )
                        for k, v in (platforms or {}).items()
                        if v.get("state") == "connected"
                    )
                )
                if (
                    st["gateway_state"] == "running"
                    and st["pid"] == pid
                    and (pid != oldpid)
                    and (st["code_sha"] == sha)
                    and (0 <= time.time() - st["answered_at"] < 15)
                    and (ident["hermes_home"] == str(self.cfg))
                    and connected
                ):
                    generation(st)
                    return (st, ident)
            except (OSError, ValueError, AssertionError, subprocess.SubprocessError):
                LOG.warning("readiness probe not ready", exc_info=True)
            self.budget.pause(3)
        raise RuntimeError("fresh identity/readiness failed: " + sha)

    def clear_owned_drain(self):
        marker = self.cfg / ".drain_request.json"
        if marker.exists():
            self.guard.check()
            assert (
                json.loads(marker.read_text(encoding="utf-8-sig")).get("principal")
                == self.principal
            ), "foreign drain owner"
            self.cmd(
                [
                    self.old_python,
                    "-c",
                    "from gateway.drain_control import clear_drain_request;clear_drain_request()",
                ],
                cwd=OLD,
            )

    def drain(self):
        self.guard.check()
        assert not (self.cfg / ".drain_request.json").exists(), "existing drain owner"
        self.cmd(
            [
                self.old_python,
                "-c",
                "from gateway.drain_control import write_drain_request;write_drain_request(principal="
                + repr(self.principal)
                + ",suppress_notification=True)",
            ],
            cwd=OLD,
        )
        deadline = time.monotonic() + self.budget.remaining(240)
        while time.monotonic() < deadline:
            self.guard.check()
            x = self.ctl("status")
            assert generation(x) == self.prepared["original"]["gateway"]
            if x["gateway_state"] == "draining" and x["active_agents"] == 0:
                return
            self.budget.pause(2)
        raise RuntimeError("drain timeout")

    def snapshot(self):
        self.budget.remaining()
        state = unit_state(self.cmd, self.unit)
        state["loaded"].pop("ControlGroup", None)
        state["physical"][str(self.drop)] = file_identity(self.drop)
        pid = int(
            self.cmd(
                ["systemctl", "--user", "show", self.unit, "-p", "MainPID", "--value"]
            ).strip()
        )
        if pid:
            gateway = generation(self.ctl("status"))
            assert gateway["pid"] == pid
        else:
            group = self.cmd(
                [
                    "systemctl",
                    "--user",
                    "show",
                    self.unit,
                    "-p",
                    "ControlGroup",
                    "--value",
                ]
            ).strip()
            assert cgroup_empty(group or self.original_cgroup), (
                "gateway cgroup still populated"
            )
            gateway = None
        return {
            "unit": state,
            "gateway": gateway,
            "plugin": tree_identity(self.live_plugin),
            "original_plugin": tree_identity(self.backup / "original-plugin"),
            "rejected_plugin": tree_identity(self.backup / "rejected-plugin"),
            "launcher": file_identity(self.launcher),
        }

    def change(self, phase, updates, action):
        expected = self.guard.check()
        expected.update(updates)
        self.guard.change(phase, expected, action)

    def stop_gateway(self):
        self.change(
            "gateway-stop",
            {"gateway": None},
            lambda: self.cmd(["systemctl", "--user", "stop", self.unit]),
        )

    def restore_plugin(self):
        plugin_restore(
            self.live_plugin,
            self.backup,
            self.prepared["original"]["plugin"],
            self.prepared["candidate_plugin"],
            self.change,
        )

    def reload_to(self, target):
        current = self.guard.check()
        expected = copy.deepcopy(current)
        expected["unit"]["loaded"] = copy.deepcopy(target["loaded"])
        self.guard.change(
            "daemon-reload",
            expected,
            lambda: self.cmd(["systemctl", "--user", "daemon-reload"]),
        )

    def start_gateway(self, sha):
        # Until the process identity is captured, recovery cannot claim a new writer.
        self.guard.check()
        self.cmd(["systemctl", "--user", "start", self.unit])
        st, ident = self.ready(sha, platforms=self.prepared["platforms"])
        actual = self.snapshot()
        previous = read_json(self.guard.path)["allowed"]
        assert all(
            {k: v for k, v in actual.items() if k != "gateway"}
            == {k: v for k, v in old.items() if k != "gateway"}
            for old in previous
        )
        assert actual["gateway"] == generation(st)
        self.guard.allow("gateway-started", [actual])
        return (st, ident)

    def rollback(self):
        if not self.recovery_started:
            self.budget = Deadline(RECOVERY_SECONDS)
            self.recovery_started = True
        owner, self.prepared = validate_owner(ROOT, self.work)
        self.original_cgroup = self.prepared["gateway_cgroup"]
        if committed_owned(ROOT, self.work):
            return
        assert decision(self.work) != "commit-" + owner["nonce"], (
            "commit reservation requires receipt reconciliation"
        )
        claim_decision(self.work, owner["nonce"], "recover")
        self.guard.check()
        validate_launcher_backup(
            self.launcher, self.backup, self.prepared["original"]["launcher"]
        )
        if (self.backup / "state-mutation-started").exists():
            assert (
                digest((self.backup / "config.yaml").read_bytes())
                == self.prepared["config_sha256"]
            )
        if self.rolled.exists():
            receipt = read_json(self.rolled)
            assert receipt["nonce"] == owner["nonce"]
            self.disarm()
            return
        if (self.backup / "new-runtime-start-attempted").exists():
            try:
                current = self.ctl("status")
            except (OSError, ValueError, AssertionError):
                current = None
            if current is not None:
                current_generation = generation(current)
                marker = self.cfg / ".drain_request.json"
                if marker.exists():
                    assert (
                        json.loads(marker.read_text(encoding="utf-8-sig")).get(
                            "principal"
                        )
                        == self.principal
                    ), "foreign drain"
                else:
                    self.cmd(
                        [
                            VENV / "bin/python",
                            "-c",
                            "from gateway.drain_control import write_drain_request;write_drain_request(principal="
                            + repr(self.principal)
                            + ",suppress_notification=True)",
                        ]
                    )
                deadline = time.monotonic() + self.budget.remaining(240)
                while time.monotonic() < deadline:
                    self.guard.check()
                    state = self.ctl("status")
                    assert generation(state) == current_generation, (
                        "recovery identity drift"
                    )
                    if (
                        state["gateway_state"] == "draining"
                        and state["active_agents"] == 0
                    ):
                        break
                    self.budget.pause(2)
                else:
                    raise RuntimeError(
                        "recovery could not drain; preserve runtime for reconciliation"
                    )
        self.stop_gateway()
        self.guard.check()
        if (self.backup / "state-mutation-started").exists():
            if not (self.backup / "new-runtime-start-attempted").exists():
                restore_dbs(self.cfg, self.backup, self.budget)
            else:
                rejected = self.backup / "rejected-runtime-state"
                rejected.mkdir(exist_ok=True)
                for n, present in json.loads(
                    (self.backup / "db-manifest.json").read_text(encoding="utf-8-sig")
                ).items():
                    if (self.cfg / n).exists():
                        backup_sqlite(
                            self.cfg / n, rejected / n.replace("/", "-"), self.budget
                        )
                check = self.cmd(
                    [
                        self.old_python,
                        "-m",
                        "hermes_cli.main",
                        "sessions",
                        "repair",
                        "--check-only",
                    ],
                    timeout=240,
                    cwd=OLD,
                )
                atomic(self.backup / "old-runtime-db-check.log", check.encode())
            assert (
                digest((self.backup / "config.yaml").read_bytes())
                == self.prepared["config_sha256"]
            )
            atomic(self.cfg / "config.yaml", (self.backup / "config.yaml").read_bytes())
        self.guard.check()
        self.clear_owned_drain()
        current = self.guard.check()
        expected = copy.deepcopy(current)
        expected["unit"]["physical"][str(self.drop)] = None
        reloaded = copy.deepcopy(expected)
        reloaded["unit"]["loaded"] = copy.deepcopy(
            self.prepared["original"]["unit"]["loaded"]
        )
        self.guard.change(
            "drop-remove",
            expected,
            lambda: self.drop.unlink(missing_ok=True),
            alternatives=[reloaded],
        )
        self.reload_to(self.prepared["original"]["unit"])
        self.restore_plugin()
        self.change(
            "launcher-restore",
            {"launcher": self.prepared["original"]["launcher"]},
            lambda: restore_launcher(
                self.launcher, self.backup, self.prepared["original"]["launcher"]
            ),
        )
        st, ident = self.start_gateway(OLD_SHA)
        atomic(
            self.rolled,
            json.dumps(
                {
                    "time": time.time(),
                    "gateway": st,
                    "identity": ident,
                    "attempt": self.attempt,
                    "old_sha": OLD_SHA,
                    "nonce": self.prepared["nonce"],
                },
                indent=2,
            ).encode(),
        )
        self.disarm()

    def save(self):
        atomic(
            self.work / "activation-result.json", json.dumps(self.d, indent=2).encode()
        )

    def run_command(self, name, args, timeout=300):
        self.d["phase"] = name
        self.save()
        try:
            out = self.cmd(args, timeout)
        except subprocess.CalledProcessError as e:
            atomic(self.work / (name + ".log"), e.output.encode())
            raise
        atomic(self.work / (name + ".log"), out.encode())
        self.d["steps"][name] = 0
        self.save()
        return out

    def install_launcher(self):
        atomic(self.launcher, self.launcher_body, mode=493)

    def run(self):
        if self.rollback_only:
            self.before = {}
            self.rollback()
            return 0
        assert not self.work.exists() and file_identity(self.drop) is None, (
            "existing activation requires reconciliation"
        )
        assert self.task_id and self.context_id, "task_id and context_id required"
        assert not (ROOT / "attempt-owner.json").exists(), (
            "existing owner requires explicit reconciliation"
        )
        self.force = "--force-rollback-after-ready" in sys.argv
        self.d = {
            "attempt": self.attempt,
            "status": "activating",
            "started": time.time(),
            "base": BASE,
            "head": SHA,
            "plugin_head": PLUGIN_SHA,
            "steps": {},
            "backup": str(self.backup),
            "unit": self.unit,
            "route_token": secrets.token_hex(32),
            "nonce": secrets.token_hex(32),
        }
        self.mutated = False
        self.armed = False
        self.before = {}
        try:
            self.preflight()
            self.prepare()
            self.cutover()
            return self.accept_route()
        except Exception as e:
            LOG.exception("activation held; checking owned recovery")
            if self.committed.exists() and committed_owned(ROOT, self.work):
                return 0
            self.d["status"] = "blocked"
            self.d["error"] = repr(e)
            if self.mutated:
                try:
                    self.rollback()
                    self.d["rollback"] = "verified"
                    self.d["status"] = "rolled_back"
                except Exception as x:
                    LOG.exception("recovery held for reconciliation")
                    self.d["rollback_error"] = repr(x)
            elif self.prepared is not None:
                self.clear_owned_drain()
                if self.armed:
                    self.disarm()
            self.d["finished"] = time.time()
            self.save()
            return 1

    def preflight(self):
        self.work.mkdir(mode=448, parents=True)
        self.save()
        val = json.loads(
            (ROOT / "reviewed-validation.json").read_text(encoding="utf-8-sig")
        )
        assert val["status"] == "validated_reviewed_candidate_not_activated"
        for n in [
            "state-suite",
            "connect-vault-suite",
            "directsdk-offline",
            "packaging",
        ]:
            assert val["steps"][n]["rc"] == 0
        # Exact dependency-only child of the reviewed reconciliation merge.
        # Keep both composition identity and its exact release-parent fence.
        assert (
            self.cmd(["git", "-C", SRC, "rev-parse", "HEAD"]).strip() == SHA
            and self.cmd(["git", "-C", SRC, "rev-parse", "HEAD^"]).strip() == COMPOSITION_SHA
            and self.cmd(["git", "-C", SRC, "rev-parse", "HEAD^^2"]).strip() == BASE
        )
        assert not self.cmd(["git", "-C", SRC, "status", "--porcelain"]).strip()
        assert self.cmd(
            ["git", "-C", REVIEWED_PLUGIN, "rev-parse", "HEAD"]
        ).strip() == PLUGIN_SHA and (
            not self.cmd(
                ["git", "-C", REVIEWED_PLUGIN, "status", "--porcelain"]
            ).strip()
        )
        assert self.cmd(
            ["git", "-C", OLD, "rev-parse", "HEAD"]
        ).strip() == OLD_SHA and (
            not self.cmd(["git", "-C", OLD, "status", "--porcelain"]).strip()
        )
        assert (
            self.cmd(["git", "-C", self.live_plugin, "rev-parse", "HEAD"]).strip()
            == OLD_PLUGIN_SHA
        )
        live_status = set(
            self.cmd(
                ["git", "-C", self.live_plugin, "status", "--porcelain"]
            ).splitlines()
        )
        assert (
            live_status
            <= {"M model_catalog.py", " M model_catalog.py", "?? .hermes-catalog.json"}
            and {"M model_catalog.py", " M model_catalog.py"} & live_status
        )
        assert (self.live_plugin / "model_catalog.py").read_bytes() == (
            REVIEWED_PLUGIN / "model_catalog.py"
        ).read_bytes()
        assert shutil.disk_usage(self.h).free > 4 * 1024**3 + sum(
            (self.cfg / n).stat().st_size * 2 for n in DBS if (self.cfg / n).exists()
        )
        self.d["executor"] = {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "cgroup": pathlib.Path("/proc/self/cgroup").read_text(encoding="utf-8-sig"),
        }
        assert (
            self.activation_unit in self.d["executor"]["cgroup"]
            and "hermes-gateway.service" not in self.d["executor"]["cgroup"]
        )
        self.before = self.ctl("status")
        self.initial_generation = generation(self.before)
        assert (
            self.before["gateway_state"] == "running"
            and self.before["active_agents"] == 0
            and (self.before["code_sha"] == OLD_SHA)
            and (0 <= time.time() - self.before["answered_at"] < 15)
        )
        self.d["before"] = self.before
        self.pid = self.before["pid"]
        assert (
            int(
                self.cmd(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        self.unit,
                        "-p",
                        "MainPID",
                        "--value",
                    ]
                )
            )
            == self.pid
        )
        argv = (
            pathlib.Path("/proc/" + str(self.pid) + "/cmdline")
            .read_bytes()
            .decode()
            .strip("\x00")
            .split("\x00")
        )
        assert (
            len(argv) >= 5
            and argv[0] == str(self.old_store_python)
            and (argv[1:3] == ["-I", "-c"])
            and (str(OLD) in argv[3])
            and ("hermes_bootstrap" in argv[3])
            and (argv[-2:] == ["gateway", "run"])
        ), "unexpected old process argv"
        self.prod_env = dict(
            x.split("=", 1)
            for x in pathlib.Path("/proc/" + str(self.pid) + "/environ")
            .read_bytes()
            .decode()
            .strip("\x00")
            .split("\x00")
            if "=" in x
        )
        assert self.prod_env.get("HERMES_HOME", str(self.cfg)) == str(self.cfg)
        node_bin = str(next((ROOT / "stage-runtime").glob("node-*/bin")))
        self.newpath = (
            str(VENV / "bin")
            + ":"
            + str(SRC / ".hermes/bin")
            + ":"
            + node_bin
            + ":/home/jimmy/.local/bin:/usr/local/bin:/usr/bin:/bin"
        )
        procs = self.cmd(["ps", "-u", str(unix_uid()), "-o", "pid,ppid,comm"])
        self.d["preexisting_processes"] = procs
        busy = []
        for line in procs.splitlines()[1:]:
            parts = line.split()
            if (
                len(parts) >= 3
                and parts[-1]
                in {
                    "codex",
                    "claude",
                    "opencode",
                    "npm",
                    "pytest",
                    "make",
                    "terraform",
                    "kubectl",
                }
                and (int(parts[1]) != self.pid)
            ):
                busy.append(line)
        assert not busy, ("busy workload", busy)
        if (self.cfg / "kanban.db").exists():
            c = sqlite3.connect(
                "file:" + str(self.cfg / "kanban.db") + "?mode=ro", uri=True
            )
            rows = c.execute(
                "select status,count(*) from tasks group by status"
            ).fetchall()
            c.close()
            assert not any(
                (
                    n and self.st in ("running", "in_progress", "claimed")
                    for self.st, n in rows
                )
            ), "busy kanban"

    def prepare(self):
        preexist = sorted(p.name for p in self.drop.parent.glob("*.conf"))
        assert preexist and preexist[-1] < self.drop.name, (
            "drop-in precedence",
            preexist,
        )
        self.backup.mkdir(mode=448)
        self.isolated = self.work / "isolated-config"
        self.isolated.mkdir(mode=448)
        shutil.copy2(self.cfg / "config.yaml", self.isolated / "config.yaml")
        self.migration = "from hermes_cli.config import migrate_config,check_config_version; migrate_config(interactive=False,quiet=False); versions=check_config_version(raise_on_parse_error=True); print(versions); assert versions[0]==versions[1], versions"
        self.env["HERMES_HOME"] = str(self.isolated)
        self.run_command(
            "isolated-migrate", [VENV / "bin/python", "-c", self.migration]
        )
        self.run_command(
            "isolated-config-check", [VENV / "bin/hermes", "config", "check"]
        )
        self.env["HERMES_HOME"] = str(self.cfg)
        lm = {
            "exists": self.launcher.exists() or self.launcher.is_symlink(),
            "symlink": self.launcher.is_symlink(),
            "target": os.readlink(self.launcher)
            if self.launcher.is_symlink()
            else None,
            "mode": self.launcher.stat().st_mode & 511
            if self.launcher.exists()
            else 493,
        }
        atomic(self.backup / "launcher.json", json.dumps(lm).encode())
        if self.launcher.exists() and (not self.launcher.is_symlink()):
            shutil.copy2(self.launcher, self.backup / "launcher")
        self.original_cgroup = self.cmd(
            ["systemctl", "--user", "show", self.unit, "-p", "ControlGroup", "--value"]
        ).strip()
        assert self.original_cgroup
        original = self.snapshot()
        assert original["gateway"] == self.initial_generation, (
            "gateway changed during preparation"
        )
        validate_launcher_backup(self.launcher, self.backup, original["launcher"])
        worker_pid = int(
            self.cmd(
                [
                    "systemctl",
                    "--user",
                    "show",
                    self.activation_unit,
                    "-p",
                    "MainPID",
                    "--value",
                ]
            )
        )
        assert worker_pid == os.getpid(), (
            "activation must directly own its main process"
        )
        worker_invocation = self.cmd(
            [
                "systemctl",
                "--user",
                "show",
                self.activation_unit,
                "-p",
                "InvocationID",
                "--value",
            ]
        ).strip()
        assert re.fullmatch("[0-9a-f]{32}", worker_invocation)
        self.prepared = {
            "schema": SCHEMA,
            "attempt": self.attempt,
            "nonce": self.d["nonce"],
            "activation_unit": self.activation_unit,
            "original": original,
            "platforms": self.before.get("platforms", {}),
            "gateway_cgroup": self.original_cgroup,
            "candidate_plugin": tree_identity(REVIEWED_PLUGIN),
            "worker_start": proc_start(os.getpid()),
            "worker_pid": os.getpid(),
            "worker_identity": {
                "unit": unit_state(self.cmd, self.activation_unit),
                "invocation": worker_invocation,
            },
            "config_sha256": digest((self.cfg / "config.yaml").read_bytes()),
        }
        write_json(self.backup / "prepared.json", self.prepared)
        write_json(
            ROOT / "attempt-owner.json",
            {
                "schema": SCHEMA,
                "attempt": self.attempt,
                "nonce": self.d["nonce"],
                "activation_unit": self.activation_unit,
                "prepared_sha256": digest((self.backup / "prepared.json").read_bytes()),
            },
        )
        self.guard.allow("prepared", [original])
        validate_migration(
            yaml_value(self.cfg / "config.yaml", self.cmd, VENV / "bin/python"),
            yaml_value(self.isolated / "config.yaml", self.cmd, VENV / "bin/python"),
            yaml_value(self.isolated / "config.yaml", self.cmd, VENV / "bin/python"),
        )
        watchdog_path = pathlib.Path(__file__).with_name(
            "jimmy-watchdog.py"
            if pathlib.Path(__file__).name == "jimmy-activate.py"
            else "jimmy_v0231_watchdog.py"
        )
        assert watchdog_path.is_file(), "missing paired watchdog"
        self.run_command(
            "watchdog-import",
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                "import runpy,sys; runpy.run_path(sys.argv[1], run_name='paired_watchdog_probe')",
                watchdog_path,
            ],
        )
        self.run_command(
            "arm-rollback",
            [
                "systemd-run",
                "--user",
                "--unit=" + self.timer,
                "--on-active=1800s",
                "--timer-property=AccuracySec=1s",
                "--property=RuntimeMaxSec=1500",
                "--property=MemoryMax=512M",
                "/usr/bin/python3",
                watchdog_path,
                self.attempt,
                self.activation_unit,
            ],
        )
        self.armed = True
        assert (
            self.cmd(
                ["systemctl", "--user", "is-active", self.timer + ".timer"]
            ).strip()
            == "active"
        )

    def cutover(self):
        self.drain()
        assert self.ctl("status")["active_agents"] == 0
        self.mutated = True
        self.d["phase"] = "stop"
        self.save()
        self.stop_gateway()
        self.guard.check()
        assert not pathlib.Path("/proc/" + str(self.pid)).exists(), (
            "old writer survived stop"
        )
        self.clear_owned_drain()
        shutil.copy2(self.cfg / "config.yaml", self.backup / "config.yaml")
        manifest = {}
        for n in DBS:
            manifest[n] = (self.cfg / n).exists()
            if manifest[n]:
                backup_sqlite(
                    self.cfg / n, self.backup / n.replace("/", "-"), self.budget
                )
        atomic(self.backup / "db-manifest.json", json.dumps(manifest).encode())
        atomic(self.backup / "state-mutation-started", b"1")
        plugin_install(
            self.live_plugin,
            self.backup,
            REVIEWED_PLUGIN,
            self.prepared["original"]["plugin"],
            self.prepared["candidate_plugin"],
            self.change,
        )
        assert self.cmd(
            ["git", "-C", self.live_plugin, "rev-parse", "HEAD"]
        ).strip() == PLUGIN_SHA and (
            not self.cmd(
                ["git", "-C", self.live_plugin, "status", "--porcelain"]
            ).strip()
        )
        self.env.update(self.prod_env)
        self.env.update(
            HERMES_HOME=str(self.cfg),
            HERMES_RUNTIME_DIR=str(ROOT / "stage-runtime"),
            VIRTUAL_ENV=str(VENV),
            PATH=self.newpath,
        )
        self.env.pop("PYTHONPATH", None)
        self.env.pop("PYTHONHOME", None)
        self.guard.check()
        self.mutated = True
        self.run_command("live-migrate", [VENV / "bin/python", "-c", self.migration])
        validate_migration(
            yaml_value(self.backup / "config.yaml", self.cmd, VENV / "bin/python"),
            yaml_value(self.isolated / "config.yaml", self.cmd, VENV / "bin/python"),
            yaml_value(self.cfg / "config.yaml", self.cmd, VENV / "bin/python"),
        )
        self.run_command("live-config-check", [VENV / "bin/hermes", "config", "check"])
        self.d["schema"] = self.run_command(
            "schema", [VENV / "bin/hermes", "config", "get", "_config_version"]
        ).strip()
        # Upgrade the stopped, backed-up store through canonical startup before
        # the read-only health/drift gate; config migration does not open state.db.
        self.run_command(
            "state-schema",
            [VENV / "bin/python", "-c", "from hermes_state import SessionDB; db = SessionDB(); db.close()"],
            600,
        )
        self.run_command(
            "pre-session-check",
            [VENV / "bin/hermes", "sessions", "repair", "--check-only"],
            600,
        )
        override_environment = {
            "VIRTUAL_ENV": str(VENV),
            "HERMES_RUNTIME_DIR": str(ROOT / "stage-runtime"),
            "PATH": self.newpath,
            "HERMES_HOME": str(self.cfg),
        }
        body = drop_body(VENV, override_environment)
        candidate_unit = expected_unit(
            self.guard.check()["unit"],
            self.drop,
            body,
            VENV,
            override_environment,
        )
        current = self.guard.check()
        expected = copy.deepcopy(current)
        expected["unit"]["physical"] = candidate_unit["physical"]
        # An inactive unit may be garbage-collected and reloaded by systemd
        # between observations. Accept only the exact predicted old/new load.
        reloaded = copy.deepcopy(expected)
        reloaded["unit"]["loaded"] = candidate_unit["loaded"]
        self.guard.change(
            "drop-install",
            expected,
            lambda: atomic(self.drop, body),
            alternatives=[reloaded],
        )
        self.reload_to(candidate_unit)
        self.d["cutover_at"] = time.time()
        atomic(
            self.backup / "new-runtime-start-attempted",
            str(self.d["cutover_at"]).encode(),
        )
        self.st, self.ident = self.start_gateway(SHA)
        self.d["gateway"] = self.st
        self.d["identity"] = self.ident
        newargv = (
            pathlib.Path("/proc/" + str(self.st["pid"]) + "/cmdline")
            .read_bytes()
            .decode()
            .strip("\x00")
            .split("\x00")
        )
        assert newargv == [
            str(VENV / "bin/python"),
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
        ]
        self.d["argv"] = newargv
        self.run_command(
            "session-check", [VENV / "bin/hermes", "sessions", "repair", "--check-only"]
        )
        self.run_command("plugins-list", [VENV / "bin/hermes", "plugins", "list"])
        self.run_command("version", [VENV / "bin/hermes", "--version"])
        try:
            self.run_command("doctor", [VENV / "bin/hermes", "doctor"])
        except subprocess.CalledProcessError as diagnostic:
            self.d["doctor_rc"] = diagnostic.returncode
            self.save()
        if self.force:
            raise RuntimeError(
                "intentional rollback-first proof after candidate readiness"
            )

    def accept_route(self):
        self.guard.check()
        self.st = self.ctl("status")
        assert (
            generation(self.st) == read_json(self.guard.path)["allowed"][0]["gateway"]
        )
        self.challenge = {
            "attempt": self.attempt,
            "nonce": self.d["nonce"],
            "token": self.d["route_token"],
            "task_id": self.task_id,
            "context_id": self.context_id,
            **generation(self.st),
            "requested_at": time.time(),
        }
        write_json(self.work / "awaiting-route.json", self.challenge)
        self.d["status"] = "awaiting_external_route_verdict"
        self.save()
        deadline = time.monotonic() + self.budget.remaining(ROUTE_TTL)
        while time.monotonic() < deadline:
            self.guard.check()
            verdict = self.work / "route-verdict.json"
            if verdict.exists():
                v = read_json(verdict)
                validate_route(self.challenge, v, generation(self.ctl("status")))
                break
            self.budget.pause(3)
        else:
            raise RuntimeError("external route verdict deadline")
        self.d["route_verdict"] = v
        self.st, self.ident = self.ready(
            SHA, self.before["pid"], self.before.get("platforms", {})
        )
        settle = time.monotonic() + self.budget.remaining(120)
        while self.st["active_agents"] != 0 and time.monotonic() < settle:
            self.budget.pause(2)
            self.st = self.ctl("status")
            assert {k: self.st[k] for k in ("pid", "start_time", "code_sha")} == {
                k: self.challenge[k] for k in ("pid", "start_time", "code_sha")
            }
        assert self.st["active_agents"] == 0, "routed agent did not settle"
        self.launcher.parent.mkdir(parents=True, exist_ok=True)
        self.launcher_body = (
            '#!/usr/bin/env bash\nunset PYTHONPATH PYTHONHOME\nexec "'
            + str(VENV / "bin/hermes")
            + '" "$@"\n'
        ).encode()
        self.change(
            "launcher-install",
            {
                "launcher": {
                    "kind": "file",
                    "sha256": digest(self.launcher_body),
                    "mode": 493,
                }
            },
            self.install_launcher,
        )
        self.d["version"] = self.run_command(
            "launcher-version", [self.launcher, "--version"]
        )
        self.guard.check()
        self.st = self.ctl("status")
        live = generation(self.st)
        assert self.st["active_agents"] == 0 and self.st["gateway_state"] == "running"
        validate_route(self.challenge, v, live)
        self.d["gateway"] = self.st
        self.d["identity"] = self.ident
        self.d["status"] = "activated_ready_route_verified"
        self.d["finished"] = time.time()
        validate_route(self.challenge, v, live)
        commit_receipt(ROOT, self.work, self.d)
        try:
            self.disarm()
        except Exception as disarm_error:
            LOG.exception("commit durable; timer disarm failed")
            write_json(self.work / "disarm-error.json", {"error": repr(disarm_error)})
        return 0


def execute(attempt, budget, rollback_only=False, task_id=None, context_id=None):
    return Controller(attempt, budget, rollback_only, task_id, context_id).run()


RECOVERY_SECONDS = 1050
ROUTE_TTL = 120
SCHEMA = 2


class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def remaining(self, local=None):
        remaining = self.end - time.monotonic()
        if remaining <= 0 or (local is not None and local <= 0):
            raise TimeoutError("global maintenance deadline exhausted")
        return remaining if local is None else min(local, remaining)

    def pause(self, seconds):
        time.sleep(self.remaining(seconds))


def unix_uid():
    if hasattr(os, "getuid"):
        # Guarded Linux-only maintenance operation.
        return os.getuid()  # windows-footgun: ok
    raise RuntimeError("Jimmy maintenance requires POSIX uid support")


class UnfencedCommand(BaseException):
    """Do not enter recovery until the independent supervisor fences us."""


def command(args, deadline, timeout=300, **kwargs):
    """Kill and boundedly reap commands before any caller enters recovery."""
    if not hasattr(os, "killpg") or not hasattr(signal, "SIGKILL"):
        raise RuntimeError("Jimmy maintenance requires POSIX process groups")
    # Reserve one second inside this same global budget for termination.
    remaining = deadline.remaining() - 1
    if remaining <= 0:
        raise TimeoutError("insufficient deadline for command and termination")
    process = subprocess.Popen(
        list(map(str, args)),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )
    try:
        try:
            output, _ = process.communicate(timeout=min(timeout, remaining))
        except BaseException:
            try:
                # Both POSIX attributes are required by the guard above.
                os.killpg(  # windows-footgun: ok
                    process.pid,
                    signal.SIGKILL,  # windows-footgun: ok
                )
            except ProcessLookupError:
                LOG.debug("timed-out command group already exited")
            try:
                process.wait(timeout=deadline.remaining())
            except (TimeoutError, subprocess.TimeoutExpired) as error:
                raise UnfencedCommand(
                    "command reap deadline; supervisor must fence"
                ) from error
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, args, output)
        return output
    finally:
        # Popen.__exit__ waits without a timeout; do not use its context manager.
        process.stdout.close()


@contextlib.contextmanager
def attempt_lock(path, deadline):
    with pathlib.Path(path).open("a", encoding="utf-8") as lock:
        while True:
            deadline.remaining()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                deadline.pause(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_identity(path):
    path = pathlib.Path(path)
    if path.is_symlink():
        return {
            "kind": "symlink",
            "target": os.readlink(path),
            "resolved_sha256": digest(path.read_bytes()) if path.is_file() else None,
        }
    if not path.exists():
        return None
    assert path.is_file(), ("not regular file", str(path))
    return {
        "kind": "file",
        "sha256": digest(path.read_bytes()),
        "mode": path.stat().st_mode & 511,
    }


def tree_identity(path):
    """Include git metadata and untracked files; exclude only generated Python caches."""
    path = pathlib.Path(path)
    if not path.exists():
        return None
    assert path.is_dir() and (not path.is_symlink()), "unexpected plugin link"
    entries = {".": {"kind": "directory", "mode": path.stat().st_mode & 0o777}}
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path)
        if "__pycache__" in relative.parts or item.suffix in {".pyc", ".pyo"}:
            continue
        if item.is_symlink() or item.is_file():
            entries[str(relative)] = file_identity(item)
        elif item.is_dir():
            entries[str(relative)] = {
                "kind": "directory",
                "mode": item.stat().st_mode & 511,
            }
        else:
            raise AssertionError("unsupported plugin entry")
    return digest(json.dumps(entries, sort_keys=True).encode())


def proc_start(pid):
    text = pathlib.Path("/proc", str(pid), "stat").read_text(encoding="utf-8-sig")
    return int(text.rsplit(")", 1)[1].split()[19])


def generation(status):
    pid = status["pid"]
    start = proc_start(pid)
    assert status["start_time"] == start and status.get("answering_pid", pid) == pid
    assert 0 <= time.time() - status["answered_at"] < 15
    return {"pid": pid, "start_time": start, "code_sha": status["code_sha"]}


def cgroup_empty(group):
    assert group.startswith("/") and ".." not in pathlib.PurePosixPath(group).parts
    hierarchy = pathlib.Path("/sys/fs/cgroup")
    assert (hierarchy / "cgroup.controllers").is_file(), (
        "unified cgroup hierarchy required"
    )
    root = hierarchy / group.lstrip("/")
    assert not root.exists() or (root / "cgroup.procs").is_file(), "unreadable cgroup"
    return not root.exists() or all(
        not p.read_text(encoding="utf-8-sig").strip()
        for p in root.rglob("cgroup.procs")
    )


UNIT_PROPERTIES = (
    "FragmentPath",
    "DropInPaths",
    "ExecStart",
    "ExecStop",
    "ExecStopPost",
    "Environment",
    "Type",
    "KillMode",
    "Restart",
    "WorkingDirectory",
    "User",
    "ControlGroup",
    "ExecStartPre",
    "ExecStartPost",
    "ExecCondition",
    "ExecReload",
    "EnvironmentFiles",
    "PassEnvironment",
    "UnsetEnvironment",
    "KillSignal",
    "SendSIGKILL",
    "TimeoutStopUSec",
    "RuntimeMaxUSec",
    "RemainAfterExit",
    "RootDirectory",
)


def unit_state(cmd, unit):
    output = cmd(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--all",
            "--property=" + ",".join(UNIT_PROPERTIES),
        ]
    )
    props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    # systemd 252 omits empty command/environment-file arrays even with --all.
    # Only these well-defined empty arrays may be absent; scalar safety fields
    # must still be reported and unknown/nonempty command formats fail below.
    for key in (
        "ExecStartPre",
        "ExecStartPost",
        "ExecCondition",
        "ExecReload",
        "ExecStop",
        "ExecStopPost",
        "EnvironmentFiles",
    ):
        props.setdefault(key, "")
    assert set(props) == set(UNIT_PROPERTIES), "incomplete loaded unit properties"
    for key in (
        "ExecStart",
        "ExecStop",
        "ExecStopPost",
        "ExecStartPre",
        "ExecStartPost",
        "ExecCondition",
        "ExecReload",
    ):
        raw = props[key]
        props[key] = [
            list(m)
            for m in re.findall(
                "path=(.*?) ; argv\\[\\]=(.*?) ; ignore_errors=(yes|no)", props[key]
            )
        ]
        assert not raw or props[key], "unrecognized loaded command properties"
    props["Environment"] = sorted(shlex.split(props["Environment"]))
    props["DropInPaths"] = shlex.split(props["DropInPaths"])
    assert props["FragmentPath"], "missing loaded fragment"
    physical = {
        p: file_identity(p) for p in [props["FragmentPath"], *props["DropInPaths"]]
    }
    directories = {pathlib.Path(x).parent for x in props["DropInPaths"]}
    for base in (
        pathlib.Path.home() / ".config/systemd/user",
        pathlib.Path("/run/user") / str(unix_uid()) / "systemd/user",
        pathlib.Path("/etc/systemd/user"),
        pathlib.Path("/run/systemd/user"),
        pathlib.Path("/usr/local/lib/systemd/user"),
        pathlib.Path("/usr/lib/systemd/user"),
    ):
        directories.add(base / (unit + ".d"))
        fragment = base / unit
        if fragment.exists() or fragment.is_symlink():
            physical[str(fragment)] = file_identity(fragment)
    directories.add(pathlib.Path(props["FragmentPath"] + ".d"))
    for directory in directories:
        for path in directory.glob("*.conf"):
            physical[str(path)] = file_identity(path)
    return {"loaded": props, "physical": physical}


def drop_body(venv, environment):
    python = str(venv / "bin/python")
    lines = [
        "[Service]",
        "ExecStart=",
        "ExecStart="
        + " ".join(
            quoted(x) for x in [python, "-m", "hermes_cli.main", "gateway", "run"]
        ),
        "ExecStop=",
        "ExecStop=" + quoted(python) + " -m gateway.systemd_stop_mark",
        "ExecStopPost=",
        "ExecStopPost=-"
        + quoted(python)
        + " -m gateway.cgroup_cleanup --abort-marker-only",
    ]
    lines.extend(("Environment=" + quoted(k + "=" + v) for k, v in environment.items()))
    return ("\n".join(lines) + "\n").encode()


def expected_unit(original, drop, body, venv, environment):
    result = copy.deepcopy(original)
    loaded = result["loaded"]
    loaded["DropInPaths"] = sorted([*loaded["DropInPaths"], str(drop)])
    python = str(venv / "bin/python")
    loaded["ExecStart"] = [[python, python + " -m hermes_cli.main gateway run", "no"]]
    loaded["ExecStop"] = [[python, python + " -m gateway.systemd_stop_mark", "no"]]
    loaded["ExecStopPost"] = [
        [python, python + " -m gateway.cgroup_cleanup --abort-marker-only", "yes"]
    ]
    env = dict(x.split("=", 1) for x in loaded["Environment"])
    env.update(environment)
    loaded["Environment"] = sorted((k + "=" + v for k, v in env.items()))
    result["physical"][str(drop)] = {
        "kind": "file",
        "sha256": digest(body),
        "mode": 384,
    }
    return result


def read_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    atomic(path, json.dumps(value, sort_keys=True, indent=2).encode())


def validate_owner(root, work):
    owner = read_json(root / "attempt-owner.json")
    prepared = read_json(work / "backup/prepared.json")
    assert owner["schema"] == prepared["schema"] == SCHEMA, "legacy owner refused"
    assert owner["attempt"] == prepared["attempt"] == work.name
    assert owner["nonce"] == prepared["nonce"] and re.fullmatch(
        "[0-9a-f]{64}", owner["nonce"]
    )
    assert owner["prepared_sha256"] == digest(
        (work / "backup/prepared.json").read_bytes()
    )
    assert owner["activation_unit"] == prepared["activation_unit"]
    return (owner, prepared)


class Journal:
    """Write-ahead exact states allow retries on either side of atomic mutations."""

    def __init__(self, root, work, snapshot, time_check=lambda: None):
        self.root, self.work, self.snapshot = (root, work, snapshot)
        self.time_check = time_check
        self.path = work / "ownership-state.json"

    def check(self):
        self.time_check()
        owner, _ = validate_owner(self.root, self.work)
        state = read_json(self.path)
        assert state["nonce"] == owner["nonce"]
        actual = self.snapshot()
        self.time_check()
        assert actual in state["allowed"], ("unowned boundary", state["phase"], actual)
        return actual

    def allow(self, phase, states):
        self.time_check()
        owner, _ = validate_owner(self.root, self.work)
        write_json(
            self.path, {"nonce": owner["nonce"], "phase": phase, "allowed": states}
        )

    def change(self, phase, expected, action, alternatives=()):
        before = self.check()
        after = [expected, *alternatives]
        self.allow(phase, [before, *after])
        self.time_check()
        action()
        assert self.check() in after, ("transition mismatch", phase)
        self.allow(phase, after)


def validate_migration(original, expected, actual):
    assert isinstance(original, dict) and original.get("_config_version") == 51
    approved = copy.deepcopy(original)
    approved["_config_version"] = 53
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
    assert canonical(expected) == canonical(approved), (
        "isolated migration exceeds reviewed allowlist"
    )
    assert canonical(actual) == canonical(expected), (
        "live migration differs from reviewed migration"
    )


def yaml_value(path, cmd, python):
    return json.loads(
        cmd(
            [
                python,
                "-c",
                "import json,sys; import hermes_yaml as yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1], encoding='utf-8-sig'))))",
                path,
            ]
        )
    )


def backup_sqlite(source, destination, deadline):
    timeout = deadline.remaining(2)
    with contextlib.closing(
        sqlite3.connect("file:" + str(source) + "?mode=ro", uri=True, timeout=timeout)
    ) as src:
        with contextlib.closing(sqlite3.connect(destination, timeout=timeout)) as dst:
            src.execute("pragma busy_timeout=" + str(int(timeout * 1000)))
            dst.execute("pragma busy_timeout=" + str(int(timeout * 1000)))
            src.backup(
                dst, pages=128, progress=lambda *_: deadline.remaining(), sleep=0.01
            )
            dst.set_progress_handler(
                lambda: int(time.monotonic() >= deadline.end), 1000
            )
            assert dst.execute("pragma quick_check").fetchone()[0] == "ok"
    deadline.remaining()


def plugin_install(live, backup, candidate, original_id, candidate_id, change):
    stage = backup / "candidate-plugin"
    original = backup / "original-plugin"
    assert tree_identity(live) == original_id and (not original.exists())
    if not stage.exists():
        shutil.copytree(candidate, stage, symlinks=True)
    assert tree_identity(stage) == candidate_id, (
        "incomplete candidate staging; held before swap"
    )
    change(
        "plugin-save-original",
        {"plugin": None, "original_plugin": original_id},
        lambda: durable_rename(live, original),
    )
    change(
        "plugin-install", {"plugin": candidate_id}, lambda: durable_rename(stage, live)
    )


def plugin_restore(live, backup, original_id, candidate_id, change):
    original = backup / "original-plugin"
    rejected = backup / "rejected-plugin"
    current = tree_identity(live)
    if current == original_id:
        assert tree_identity(original) is None, (
            "duplicate original requires reconciliation"
        )
        return
    assert tree_identity(original) == original_id, (
        "missing/corrupt original; live untouched"
    )
    assert current in (None, candidate_id), "foreign live plugin"
    if current is not None:
        assert not rejected.exists(), (
            "rejected plugin already exists with live candidate"
        )
        change(
            "plugin-reject",
            {"plugin": None, "rejected_plugin": candidate_id},
            lambda: durable_rename(live, rejected),
        )
    assert tree_identity(original) == original_id
    change(
        "plugin-restore",
        {"plugin": original_id, "original_plugin": None},
        lambda: durable_rename(original, live),
    )


def validate_launcher_backup(launcher, backup, expected):
    metadata = read_json(backup / "launcher.json")
    if metadata["symlink"]:
        target = pathlib.Path(metadata["target"])
        if not target.is_absolute():
            target = launcher.parent / target
        actual = {
            "kind": "symlink",
            "target": metadata["target"],
            "resolved_sha256": digest(target.read_bytes())
            if target.is_file()
            else None,
        }
    elif metadata["exists"]:
        actual = {
            "kind": "file",
            "sha256": digest((backup / "launcher").read_bytes()),
            "mode": metadata["mode"],
        }
    else:
        actual = None
    assert actual == expected, "corrupt launcher backup; live untouched"


def restore_launcher(launcher, backup, expected):
    validate_launcher_backup(launcher, backup, expected)
    metadata = read_json(backup / "launcher.json")
    if metadata["symlink"]:
        temp = launcher.with_name(launcher.name + ".rollback-tmp")
        temp.unlink(missing_ok=True)
        temp.symlink_to(metadata["target"])
        os.replace(temp, launcher)
    elif metadata["exists"]:
        atomic(launcher, (backup / "launcher").read_bytes(), mode=metadata["mode"])
    else:
        launcher.unlink(missing_ok=True)


def validate_route(challenge, verdict, live, now=None):
    now = time.time() if now is None else now
    for key in (
        "attempt",
        "nonce",
        "token",
        "task_id",
        "context_id",
        "pid",
        "start_time",
        "code_sha",
        "requested_at",
    ):
        assert verdict.get(key) == challenge[key], ("route binding", key)
    assert challenge["task_id"] and challenge["context_id"]
    assert verdict.get("accepted") is True
    assert re.fullmatch("[0-9a-fA-F]{64}", verdict.get("reply_sha256", ""))
    assert challenge["requested_at"] <= verdict["observed_at"] <= now
    assert 0 <= now - challenge["requested_at"] <= ROUTE_TTL
    assert now - verdict["observed_at"] <= ROUTE_TTL
    assert {key: live[key] for key in ("pid", "start_time", "code_sha")} == {
        key: challenge[key] for key in ("pid", "start_time", "code_sha")
    }


def committed_owned(root, work):
    owner, _ = validate_owner(root, work)
    path = work / "activated.json"
    if not path.exists():
        return False
    receipt = read_json(path)
    assert receipt["attempt"] == owner["attempt"] and receipt["nonce"] == owner["nonce"]
    return True


def decision(work):
    path = work / "terminal-decision"
    if not path.is_symlink():
        assert not path.exists(), "invalid terminal decision"
        return None
    return os.readlink(path)


def claim_decision(work, nonce, kind):
    target = kind + "-" + nonce
    path = work / "terminal-decision"
    try:
        path.symlink_to(target)
        fd = os.open(work, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except FileExistsError:
        assert decision(work) == target, "terminal decision already reserved"


def commit_receipt(root, work, receipt):
    owner, _ = validate_owner(root, work)
    assert receipt["nonce"] == owner["nonce"] and receipt["attempt"] == owner["attempt"]
    claim_decision(work, owner["nonce"], "commit")
    write_json(work / "activated.json", receipt)


def main():
    if not __debug__:
        raise RuntimeError("controller guards require Python assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("attempt")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--force-rollback-after-ready", action="store_true")
    parser.add_argument("--task-id")
    parser.add_argument("--context-id")
    args = parser.parse_args()
    assert re.fullmatch("(?:rollback|activate)[a-z0-9-]{1,24}", args.attempt)
    assert (
        pathlib.Path.home() == pathlib.Path("/home/jimmy")
        and socket.gethostname() == "jimmy"
    )
    assert not os.getenv("_HERMES_GATEWAY")
    os.umask(63)
    budget = Deadline(RECOVERY_SECONDS if args.rollback else 2700)
    with attempt_lock(ROOT / "activation.lock", budget):
        return execute(
            args.attempt, budget, args.rollback, args.task_id, args.context_id
        )


if __name__ == "__main__":
    sys.exit(main())
