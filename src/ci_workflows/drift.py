"""drift.py — compare materialized caller files to expected render for their marker version.

Modes:
  --mode=check   exit 0 iff all callers match their registered tier template@marker-version
  --mode=badge   update README.md with a Markdown table showing per-caller version + lag
  --mode=report  emit JSON payload for dashboards (used by .forgejo/workflows/drift-report.yml)
"""

import argparse
import base64
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ci_workflows.forgejo import ForgejoClient, ForgejoError
from ci_workflows.registry import Caller, load_registry
from ci_workflows.render import cron_minute_for_repo, render_marker, render_template
from ci_workflows.sync import (
    MARKER_PATH,
    README_PATH,
    WORKFLOW_DIR,
    _regenerate_readme_badges,
)

# Sentinel value for `expected_files[README_PATH]`. When `check_one_caller`
# encounters this sentinel as the expected content, it runs a regen-parity
# check (current README vs `_regenerate_readme_badges(current)`) instead of
# a literal byte-compare. Double-underscore prefix matches the existing
# `RESOLVER_ERROR_SENTINEL_PREFIX` convention.
README_REGEN_SENTINEL = "__readme_badge_regen__"


def _build_expected(
    caller: Caller, templates_root: Path, version: str, synced: str, source_commit: str
) -> dict[str, str]:
    cron = cron_minute_for_repo(caller.repo)
    marker = render_marker(
        version=version,
        tier=caller.tier,
        source_commit=source_commit,
        synced=synced,
        synced_by=f"sync.py@{version}",
    )
    files: dict[str, str] = {MARKER_PATH: marker}
    for filename in ("ci.yml", "gitleaks-sweep.yml"):
        tpl = templates_root / caller.tier / filename
        files[f"{WORKFLOW_DIR}/{filename}"] = render_template(
            template_path=tpl,
            templates_root=templates_root,
            caller_entry=caller,
            version=version,
            synced=synced,
            cron_minute=cron if "sweep" in filename else None,
        )
    files[README_PATH] = README_REGEN_SENTINEL
    return files


# Patterns that strip volatile fields before comparing actual vs expected file
# contents. Per-file-type so workflow body lines that happen to start with
# `synced:` at column 0 don't get over-normalized.
_MARKER_PATTERNS = (
    (re.compile(r"^synced: .*$", re.MULTILINE), "synced: <volatile>"),
    (re.compile(r"^synced_by: .*$", re.MULTILINE), "synced_by: <volatile>"),
    (re.compile(r"^source_commit: .*$", re.MULTILINE), "source_commit: <volatile>"),
)
_WORKFLOW_PATTERNS = ((re.compile(r"^# Synced: .*$", re.MULTILINE), "# Synced: <volatile>"),)

# Wire format: "{PREFIX} {error_message}" — consumers split on ": " (colon-space)
# or just `.startswith(PREFIX)`. Double-underscore prefix guarantees no real path
# can collide with this sentinel.
RESOLVER_ERROR_SENTINEL_PREFIX = "__resolver_error__:"


def _normalize_volatile(text: str, *, path: str) -> str:
    # Dispatch is binary: MARKER_PATH gets marker patterns, everything else
    # (currently only workflow files under WORKFLOW_DIR) gets workflow patterns.
    # If a future caller adds a third file class to expected_files, extend here.
    patterns = _MARKER_PATTERNS if path == MARKER_PATH else _WORKFLOW_PATTERNS
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text


def _resolve_default_branch(client: ForgejoClient, owner: str, repo: str) -> str:
    info = client.get_repo(owner, repo)
    branch = info.get("default_branch")
    if not branch:
        raise ForgejoError(f"get_repo({owner}/{repo}) returned no default_branch")
    return branch


def check_one_caller(
    client: ForgejoClient, *, caller: Caller, expected_files: dict[str, str]
) -> tuple[bool, list[str]]:
    """Returns (ok, diffs) where diffs is a list of file paths that drifted.

    On resolver failure (get_repo error or no default_branch), diffs contains a
    single sentinel entry prefixed with RESOLVER_ERROR_SENTINEL_PREFIX rather
    than a real path — consumers that iterate diffs as paths should filter on
    that prefix.
    """
    owner, repo = caller.repo.split("/", 1)
    try:
        branch = _resolve_default_branch(client, owner, repo)
    except ForgejoError as e:
        return (False, [f"{RESOLVER_ERROR_SENTINEL_PREFIX} {e}"])
    diffs: list[str] = []
    for path, expected in expected_files.items():
        try:
            got = client.get_file(owner, repo, path, ref=branch)
        except ForgejoError:
            # Missing README on the sentinel-expected path is parity with
            # sync_one_caller's silent-skip; not real drift. Other missing
            # files are still drift.
            if expected == README_REGEN_SENTINEL:
                continue
            diffs.append(path)
            continue
        actual = base64.b64decode(got["content"]).decode("utf-8")
        if expected == README_REGEN_SENTINEL and _regenerate_readme_badges(actual, owner, repo) != actual:
            diffs.append(path)
        elif expected != README_REGEN_SENTINEL and _normalize_volatile(actual, path=path) != _normalize_volatile(
            expected, path=path
        ):
            diffs.append(path)
    return (len(diffs) == 0, diffs)


def _client_factory_from_env() -> Callable[[], ForgejoClient]:
    token = os.environ.get("CODEBERG_TOKEN") or ""
    return lambda: ForgejoClient(base_url="https://codeberg.org", token=token)


def main_check(*, registry_path, templates_root, version, client_factory=None) -> int:
    reg = load_registry(registry_path)
    synced = "fixed-for-render-reproducibility"
    commit = "fixed"
    client = (client_factory or _client_factory_from_env())()
    all_ok = True
    for caller in reg.callers:
        expected = _build_expected(caller, Path(templates_root), version, synced, commit)
        ok, diffs = check_one_caller(client, caller=caller, expected_files=expected)
        if not ok:
            print(f"drift: {caller.repo} — {', '.join(diffs)}")
            all_ok = False
        else:
            print(f"clean: {caller.repo}")
    return 0 if all_ok else 1


def main_report(*, registry_path, templates_root, version, client_factory=None) -> int:
    reg = load_registry(registry_path)
    synced = "fixed-for-render-reproducibility"
    commit = "fixed"
    client = (client_factory or _client_factory_from_env())()
    out = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "central_version": version,
        "callers": [],
    }
    all_ok = True
    for caller in reg.callers:
        expected = _build_expected(caller, Path(templates_root), version, synced, commit)
        ok, diffs = check_one_caller(client, caller=caller, expected_files=expected)
        if not ok:
            all_ok = False
        out["callers"].append({"repo": caller.repo, "tier": caller.tier, "ok": ok, "diffs": diffs})
    print(json.dumps(out, indent=2))
    return 0 if all_ok else 1


def main_badge(*, registry_path, templates_root, version, readme_path, client_factory=None) -> int:
    reg = load_registry(registry_path)
    synced = "fixed"
    commit = "fixed"
    client = (client_factory or _client_factory_from_env())()
    rows = []
    for caller in reg.callers:
        expected = _build_expected(caller, Path(templates_root), version, synced, commit)
        ok, _ = check_one_caller(client, caller=caller, expected_files=expected)
        badge = "🟢 clean" if ok else "🔴 drift"
        rows.append(f"| `{caller.repo}` | `{caller.tier}` | {badge} |")
    table = ["| repo | tier | drift |", "|---|---|---|", *rows]
    block = "<!-- drift-table:start -->\n" + "\n".join(table) + "\n<!-- drift-table:end -->\n"
    readme = Path(readme_path)
    text = readme.read_text()
    text = re.sub(
        r"<!-- drift-table:start -->.*<!-- drift-table:end -->\n?",
        block,
        text,
        flags=re.DOTALL,
    )
    readme.write_text(text)
    print(f"Updated drift table in {readme_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["check", "badge", "report"], required=True)
    parser.add_argument("--registry", default="callers.yml")
    parser.add_argument("--templates-root", default="templates")
    parser.add_argument("--version", required=True)
    parser.add_argument("--readme", default="README.md", help="used only by --mode=badge")
    args = parser.parse_args(argv)
    if args.mode == "check":
        return main_check(
            registry_path=Path(args.registry),
            templates_root=args.templates_root,
            version=args.version,
        )
    if args.mode == "report":
        return main_report(
            registry_path=Path(args.registry),
            templates_root=args.templates_root,
            version=args.version,
        )
    return main_badge(
        registry_path=Path(args.registry),
        templates_root=args.templates_root,
        version=args.version,
        readme_path=args.readme,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
