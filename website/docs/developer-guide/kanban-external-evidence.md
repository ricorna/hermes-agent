# Operator-authorized external evidence

Some repositories intentionally prohibit hosted Actions. An empty required-check
set still fails closed by default. An operator may instead authorize one
independently reviewed evidence manifest for one board, task, and claimed run.
This is an explicit alternative acceptance policy, not a GitHub status and not
an assertion that repository-required hosted checks ran.

## Trust boundary

The operator must review the independent verification report, archive, commands,
source identities, and limitations before pinning the manifest's SHA-256. A
worker must not authorize its own manifest. `metadata.ok`, receipt objects,
artifact paths, and policy dictionaries passed to completion cannot enable this
path. No model-facing activation tool is added.

This follows Hermes's administrative config trust model. It is NOT a security
boundary against a hostile worker with arbitrary write access to the same user's
config, verifier source, or SQLite database. For that threat model, isolate the
policy and authoritative lifecycle writer in an operator-owned service/OS identity.
A chmod or an `--operator` flag on the same UID would not solve that problem.
Do not advertise this policy as cryptographic execution attestation.

## Activation (operator only)

In the profile that will perform completion, use the normal config writer to set
`kanban.external_evidence_acceptance` to this mapping, merging any existing entries:

```yaml
"/absolute/canonical/path/to/board/kanban.db":
  t_example:
    manifest_path: /absolute/path/to/reviewed-manifest.json
    manifest_sha256: <sha256-of-exact-manifest-bytes>
    expires_at: "2026-10-20T00:00:00Z"
```

The board key is the resolved filesystem path of the connected SQLite database,
not a board environment variable or a worker-provided slug. The map is empty by
default. Avoid profile-wide or repository-wide authorizations. A revoked or expired
entry is checked again inside the terminal transaction. Frozen-policy workers use
their owner-supplied config snapshot; revocation requires replacing that snapshot
or terminating that worker, so use short expiries and a fresh operator process.

Manifest version 1 uses exactly these fields:

```json
{
  "version": 1,
  "board_db": "/absolute/canonical/path/to/board/kanban.db",
  "task_id": "t_example",
  "run_id": 123,
  "repository": "OWNER/REPO",
  "pr_number": 7,
  "pr_url": "https://github.com/OWNER/REPO/pull/7",
  "head_sha": "<40 lowercase hex>",
  "merge_sha": "<40 lowercase hex>",
  "tested_sha": "<actual tested head or final merge SHA>",
  "tree_sha": "<identical reviewed-head and merge tree SHA>",
  "artifact_path": "/absolute/path/to/reviewed-evidence.tar.gz",
  "artifact_sha256": "<64 lowercase hex>",
  "review_comment_id": 456,
  "review_author": "<GitHub login that recorded independent review>",
  "review_body_sha256": "<sha256 of exact UTF-8 GitHub comment body>",
  "checks": [{"command": "<executed verification command>", "exit_code": 0}]
}
```

The operator-pinned manifest authenticates the reviewed archive and its execution
claims. The archive digest alone does not prove a test ran. The pinned GitHub
comment records independent provenance, potentially posted by a release owner on
the reviewer's behalf; operators must assess its content, not just its existence.
Do not manufacture logs, populate checks with unexecuted commands, or call a
head test an exact-merge execution. A tested head is accepted only when fresh
GitHub reads show the same head and an identical final merge tree. Runtime/build
limitations remain part of the independent report, not silently upgraded claims.

The collector freshly reads the PR, comment, both exact commits/trees, and then
re-reads the PR/comment. It hashes real archive bytes (64 MiB maximum), never
extracts or executes archive content, rejects tampering and identity changes,
and uses the assignee-profile GitHub credentials. It makes no GitHub writes and
never queries or triggers Actions. API failure denies completion.

After operator activation, use ordinary completion with the persisted PR contract
and current run ownership. The existing prepare/record/terminal transaction,
parent checks, live-claim fence and conditional update remain authoritative.
An authorization for a previous run is not transferable to a new claim. Remove
the scoped entry after completion. Never change the contract to `local-only`,
fabricate checks, bypass live ownership, or write terminal state through SQL.

## Validation

Run `scripts/run_tests.sh tests/hermes_cli/test_kanban_external_acceptance.py
 tests/hermes_cli/test_kanban_pr_acceptance.py -q` with the supported test Python.
The suite exercises real isolated SQLite lifecycle transitions and config loading;
GitHub replies in unit tests are fixtures, not claims of live remote verification.
A real-evidence probe must use a disposable board/home; it does not substitute for
operator authorization and normal completion on the real task.
