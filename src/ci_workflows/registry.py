"""callers.yml loader with schema validation."""

from dataclasses import dataclass
from pathlib import Path

import yaml

ALLOWED_TIERS = frozenset({"python-app", "docs-static"})

# Valid job names per tier — must match the job keys in templates/<tier>/*.yml.
# Used to validate runs_on overrides in callers.yml so typos surface at load time.
TIER_JOBS = {
    "python-app": frozenset({"lint-test", "gitleaks-fast", "sweep"}),
    "docs-static": frozenset({"lint-docs", "gitleaks-fast", "sweep"}),
}

DEFAULT_RUNS_ON = "codeberg-tiny"

# Per-tier-per-job runner defaults. Resolution order for `Caller.runs_on_for(job)`:
#   1. Caller's explicit `runs_on` override (callers.yml)
#   2. TIER_DEFAULTS[tier][job]                   ← this table
#   3. DEFAULT_RUNS_ON                            ← global fallback
#
# 2026-05-12 architectural shift: every active per-push CI job runs on
# `arm64-runner` (Pi 5 self-hosted). Capacity = 3 concurrent jobs TOTAL —
# a single `runner.capacity: 3` shared across both server.connections,
# NOT per-connection (corrected 2026-05-19, verified live from
# /etc/forgejo-runner/config.yml; was wrongly "6 across two connections").
# `codeberg-tiny` is reserved for the weekly background sweep cron (low
# priority, fair-use-policy friendly) and any non-overridden default.
#
# A heavy docs-static caller (e.g. a notebook-heavy repo) can opt INTO
# arm64-runner via explicit `runs_on` — the tier default of codeberg-tiny
# works for lightweight docs-static callers.
TIER_DEFAULTS: dict[str, dict[str, str]] = {
    "python-app": {
        "lint-test": "arm64-runner",
        "gitleaks-fast": "arm64-runner",
        "sweep": "codeberg-tiny",  # weekly cron, background
    },
    "docs-static": {
        "lint-docs": "codeberg-tiny",  # light READMEs; heavy callers override
        "gitleaks-fast": "arm64-runner",
        "sweep": "codeberg-tiny",
    },
}

# Filenames written by template rendering. Used in (a) sync._sweep_stale_workflows
# to preserve them across sweeps, and (b) registry validation to reject these
# names from `preserve:` — including them in preserve is a misconfiguration trap.
TEMPLATED_WORKFLOW_FILES = frozenset({"ci.yml", "gitleaks-sweep.yml"})


class RegistryError(ValueError):
    """Raised when callers.yml fails schema validation."""


@dataclass(frozen=True)
class Caller:
    repo: str
    tier: str
    pilot: bool = False
    # Deprecated in v3.0.0: python-app template no longer references this flag
    # (uv install is fast enough that nick-fields/retry@v3 wrapping was dropped).
    # Field kept on the dataclass for backward-compat with v2 callers.yml entries;
    # scheduled for removal in a v3.1 follow-up issue. Setting retry_setup=false
    # has no behavioral effect under v3+ python-app rendering.
    retry_setup: bool = True
    # Per-job runner overrides. Stored as a sorted tuple of (job_name, label)
    # pairs to keep Caller hashable. Empty tuple = use DEFAULT_RUNS_ON for all jobs.
    runs_on: tuple[tuple[str, str], ...] = ()
    # Non-templated workflow filenames to preserve under .github/workflows/
    # during sweep. Exact filenames only (no globs, no path separators).
    # Stored as sorted tuple for hashability.
    preserve: tuple[str, ...] = ()

    def runs_on_for(self, job: str) -> str:
        """Return the runner label for `job`.

        Resolution order: explicit override → tier default → global default.
        """
        # 1. Explicit per-caller override in callers.yml
        for k, v in self.runs_on:
            if k == job:
                return v
        # 2. Tier-level default (TIER_DEFAULTS table)
        tier_default = TIER_DEFAULTS.get(self.tier, {}).get(job)
        if tier_default:
            return tier_default
        # 3. Global fallback
        return DEFAULT_RUNS_ON


@dataclass(frozen=True)
class Registry:
    version: int
    callers: tuple[Caller, ...]

    def pilot(self) -> Caller:
        pilots = [c for c in self.callers if c.pilot]
        if len(pilots) != 1:
            raise RegistryError(f"expected exactly one pilot caller, got {len(pilots)}")
        return pilots[0]

    def by_tier(self, tier: str) -> list[Caller]:
        return [c for c in self.callers if c.tier == tier]


def load_registry(path: Path) -> Registry:
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "callers" not in data:
        raise RegistryError("callers.yml missing 'callers' key")
    if data.get("version") != 1:
        raise RegistryError(f"unsupported registry version: {data.get('version')!r}")
    pilots = 0
    callers: list[Caller] = []
    for entry in data["callers"]:
        repo = entry.get("repo")
        if not repo:
            raise RegistryError(f"caller entry missing 'repo' field: {entry!r}")
        tier = entry.get("tier")
        if tier not in ALLOWED_TIERS:
            raise RegistryError(f"unknown tier {tier!r} for {repo!r}")
        pilot = bool(entry.get("pilot", False))
        if pilot:
            pilots += 1
        retry_setup_raw = entry.get("retry_setup", True)
        if not isinstance(retry_setup_raw, bool):
            raise RegistryError(f"retry_setup for {repo!r} must be a bool (true/false); got {retry_setup_raw!r}")
        runs_on_raw = entry.get("runs_on", {})
        if runs_on_raw and not isinstance(runs_on_raw, dict):
            raise RegistryError(f"runs_on for {repo!r} must be a mapping; got {type(runs_on_raw).__name__}")
        valid_jobs = TIER_JOBS[tier]
        for job_name, label in runs_on_raw.items():
            if job_name not in valid_jobs:
                raise RegistryError(
                    f"unknown job {job_name!r} in runs_on for {repo!r} (tier={tier!r}); "
                    f"valid jobs: {sorted(valid_jobs)}"
                )
            if not isinstance(label, str) or not label:
                raise RegistryError(f"runs_on.{job_name} for {repo!r} must be a non-empty string; got {label!r}")
        runs_on = tuple(sorted(runs_on_raw.items()))
        preserve_raw = entry.get("preserve") or []
        if not isinstance(preserve_raw, list):
            raise RegistryError(f"preserve for {repo!r} must be a list; got {type(preserve_raw).__name__}")
        for name in preserve_raw:
            if not isinstance(name, str) or not name:
                raise RegistryError(f"preserve entry for {repo!r} must be a non-empty string; got {name!r}")
            if "/" in name or "\\" in name:
                raise RegistryError(f"preserve entry {name!r} for {repo!r} must not contain path separators")
            if not name.endswith((".yml", ".yaml")):
                raise RegistryError(f"preserve entry {name!r} for {repo!r} must end in .yml or .yaml")
            if name in TEMPLATED_WORKFLOW_FILES:
                raise RegistryError(
                    f"preserve entry {name!r} for {repo!r} is a templated workflow name; "
                    f"preserve cannot opt files out of the template rewrite — "
                    f"the templated content will overwrite any local edits regardless. "
                    f"Rename the local file or remove it from preserve."
                )
        preserve = tuple(sorted(preserve_raw))
        callers.append(
            Caller(
                repo=repo,
                tier=tier,
                pilot=pilot,
                retry_setup=retry_setup_raw,
                runs_on=runs_on,
                preserve=preserve,
            )
        )
    if pilots != 1:
        raise RegistryError(f"registry must have exactly one pilot, found {pilots}")
    return Registry(version=1, callers=tuple(callers))
