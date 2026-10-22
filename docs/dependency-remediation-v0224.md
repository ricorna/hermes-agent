# Draft v0.22.4 dependency remediation — NOT deployable

Base: `4d04303818b61bf3aa09278af48356315515965b` (canonical NousResearch/hermes-agent).

- aiohttp: 3.14.4 in every existing extra, retaining the Matrix Linux marker.
- OAuthLib: 4.0.0 in the Google extra.
- Pillow: 13.0.0, retaining the Python >=3.14 marker.
- yarl: 1.25.1 resolver constraint (not an unconditional runtime dependency).
- browser-harness: immutable owned-fork commit `5068d73cdac0f281b3a26d94644623fd144b486a`, from https://github.com/ricorna/browser-harness/pull/1. Its runtime version remains 0.1.13; the commit, not that version string, identifies the Pillow-pin remediation.
- certifi: proposed 2026.10.19. **Do not resolve/install before the existing three-day quarantine permits it**, currently recorded as `2026-10-23T02:24:15.072385Z`.

Existing uv overrides, Python/platform markers, and `exclude-newer = "3 days"` remain intact. No CI workflows or approval labels change. No merge or deployment is authorized.

## Deliberately pending

`uv.lock` is the unmodified accepted baseline, **not** a lock for this overlay. This draft intentionally does not claim a consistent final graph. After quarantine eligibility, regenerate and validate the full lock with official PM, build a fresh PM environment on Python 3.14.7, run dependency checks, rerun affected suites and both runtime/tested audits, and independently review the final diff and exact source identities.

A separate Bob Linux compatibility experiment retains certifi 2026.5.20 and uses the exact remediated harness commit. It is partial, not an official PM generation or clean-audit acceptance. Its residual certifi advisory remains a release blocker. No production authentication, configuration, plugins, memory, models or gateway lifecycle are touched.

## Local guard

`python3 tests/pm/test_security_dependency_overlay.py` validates the intended pins, immutable harness identifier, platform markers, quarantine and retained overrides without installing anything.
