# Jimmy v0.23.1 controller regression lane

Source repair only; these tests do not authorize activation. Production runtime remains the frozen candidate `2dbdb78e2ccc3f05cd6d6acca0cfbcfd71cb0359`. The maintenance commit is **not** a new accepted runtime SHA.

## Offline suite

```sh
python3 -m unittest discover -s tests/maintenance -p 'test_jimmy*.py' -v
python3 scripts/check --base 2dbdb78e2ccc3f05cd6d6acca0cfbcfd71cb0359
```

Use Python 3.11+ for repository checks. The unittest modules are standalone so they do not import the runtime's root pytest conftest or user configuration. Linux/systemd tests skip unless explicitly opted in. The opt-in harness creates only uniquely named `hermes-jimmy-test-t-02dfc300-*` units, private fixtures, and matching user-runtime links/drop-in directories; it stops and removes its own units, resets only their failed states, and reloads the user manager. It never starts the production controller, gateway, migrations, or restores production data.

## Authorized disposable Linux/systemd lane

Only on the expressly authorized host and in a new private disposable directory containing these exact scripts/tests:

```sh
JIMMY_DISPOSABLE_SYSTEMD_TEST=t-02dfc300 python3 -m unittest discover -s tests/maintenance -p 'test_jimmy*.py' -v
```

Tests cover both active units (old loaded commands retained until explicit reload) and inactive units (systemd may GC/reload earlier), then assert exact loaded ExecStart/ExecStop/ExecStopPost, environment, physical identities and removal. They exercise production `Journal`, `unit_state`, `expected_unit`, `fence_worker`, and `supervise`. A real worker forks a SIGTERM-resistant child; the recovery fixture independently requires both PIDs gone and the cgroup empty. Committed suppression requires both processes remain alive. The recovery child is deliberately a disposable proof script, **not a production rollback drill**.

## Safe original-controller reproductions

```sh
JIMMY_ORIGINAL_CONTROLLER=/absolute/read-only/jimmy-activate.py \
  python3 tests/maintenance/baseline_original_regressions.py -v
```

This intentionally red lane AST-extracts isolated original statements/helpers; it never imports or executes the original controller entrypoint. Expected original failures: schema migration byte fence, orphan undefined rollback branch, stale route assertion, destructive original-plugin restoration. Ownership/deadline failures are covered by repaired-controller adversarial tests; there is no claim of executing the unsafe original recovery against production.

## Staging mapping (owner only, after independent review and CI)

- `scripts/maintenance/jimmy_v0231_activate.py` -> existing candidate-root `jimmy-activate.py`
- `scripts/maintenance/jimmy_v0231_watchdog.py` -> existing candidate-root `jimmy-watchdog.py`

Preserve earlier bytes/attempts before staging. The watchdog resolves the exact sibling file, and activation verifies the paired watchdog imports before arming. Canonical filenames also work for isolated tests. Never wrap either controller in an outer flock: recovery takes the single bounded internal lock.

New activation needs a unique attempt plus `--task-id` and `--context-id`. External routing must read that attempt's `awaiting-route.json`; verdict keys are `attempt`, `nonce`, `token`, `task_id`, `context_id`, `pid`, `start_time`, `code_sha`, `requested_at`, `observed_at`, `accepted`, and a 64-hex `reply_sha256`. Do not substitute historic fixed-token receipts.

## Intentional fail-closed windows / limitations

Old attempts without the new prepared/owner schema are rejected. An uncaptured startup generation (death before readiness/identity recording), a terminal commit reservation without its durable receipt, or artifact/owner drift needs explicit owner reconciliation; the controller must not guess. Do not delete or rewrite an owner record merely to make the next run pass. New attempts after a proof require owner-controlled reconciliation of the finished prior attempt, preserving all receipts.

Recovery uses one monotonic budget including lock acquisition and blocking commands, with supervisor headroom and bounded reap. Tree copying/hashing/fsync are synchronous and cannot guarantee a hard wall-clock bound on a hung filesystem; the independent systemd limits remain required. The controller intentionally preserves the existing policy: after candidate startup it restores old data only when the old reader rejects the schema **and** receipts prove no candidate-admitted work; otherwise it fails closed. This data policy still requires the parent's candidate-ready-to-old proof and is not established by these unit fixtures.
