"""sync.py — render templates into callers and open Forgejo PRs.

Two-phase: render all to in-memory plan, abort on any error, then open/update PRs.
"""

import argparse
import base64
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from ci_workflows.forgejo import ForgejoClient, ForgejoError
from ci_workflows.registry import (
    TEMPLATED_WORKFLOW_FILES,
    Caller,
    Registry,
    load_registry,
)
from ci_workflows.render import cron_minute_for_repo, render_marker, render_template

WORKFLOW_DIR = ".github/workflows"  # verified in Task 6; fall back to .forgejo/workflows if needed
MARKER_PATH = ".ci-workflows-version"
README_PATH = "README.md"
PROBE_SECRET_NAME = "SYNC_PY_PROBE"

STALE_BADGE_NAMES = frozenset({"ruff", "pytest", "gitleaks", "pre-commit", "lint", "test", "actions"})

_BADGE_RE = re.compile(
    r"\[!\[(?P<label>[^\]]+)\]"
    r"\(https://codeberg\.org/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/workflows/"
    r"(?P<workflow>[^)]+)\.yml/badge\.svg\)\]"
    r"\(https://codeberg\.org/[^/]+/[^/]+/actions\?workflow=(?P=workflow)\.yml\)"
)


def _canonical_badge(owner: str, repo: str, workflow: str) -> str:
    return (
        f"[![{workflow}](https://codeberg.org/{owner}/{repo}"
        f"/actions/workflows/{workflow}.yml/badge.svg)]"
        f"(https://codeberg.org/{owner}/{repo}/actions?workflow={workflow}.yml)"
    )


def _canonical_block(owner: str, repo: str) -> str:
    return _canonical_badge(owner, repo, "ci") + "\n" + _canonical_badge(owner, repo, "gitleaks-sweep") + "\n"


def _regenerate_readme_badges(readme_text: str, owner: str, repo: str) -> str:
    """Rewrite stale workflow badges, dedup, and insert canonical block if missing.

    Only modifies codeberg.org badges for this owner/repo; non-CI badges (license,
    framework, shields.io) are untouched.
    """

    def replace_match(m: re.Match) -> str:
        m_owner, m_repo, workflow = (
            m.group("owner"),
            m.group("repo"),
            m.group("workflow"),
        )
        if (m_owner, m_repo) != (owner, repo):
            return m.group(0)
        if workflow in STALE_BADGE_NAMES:
            return _canonical_badge(owner, repo, "ci")
        return m.group(0)

    text = _BADGE_RE.sub(replace_match, readme_text)

    lines = text.splitlines(keepends=True)
    deduped: list[str] = []
    for line in lines:
        if (
            deduped
            and line == deduped[-1]
            and line.strip().startswith("[![ci](")
            and "/actions?workflow=ci.yml" in line
        ):
            continue
        deduped.append(line)
    text = "".join(deduped)

    owner_q = re.escape(owner)
    repo_q = re.escape(repo)
    has_ci = bool(
        re.search(
            rf"codeberg\.org/{owner_q}/{repo_q}/actions/workflows/ci\.yml/badge\.svg"
            rf".*?workflow=ci\.yml",
            text,
        )
    )
    has_sweep = bool(
        re.search(
            rf"codeberg\.org/{owner_q}/{repo_q}/actions/workflows/gitleaks-sweep\.yml"
            rf"/badge\.svg.*?workflow=gitleaks-sweep\.yml",
            text,
        )
    )

    if not has_ci:
        m = re.search(r"(?m)^# .+?\n", text)
        insertion = _canonical_badge(owner, repo, "ci") + "\n"
        if not has_sweep:
            insertion += _canonical_badge(owner, repo, "gitleaks-sweep") + "\n"
        insertion += "\n"
        if m:
            idx = m.end()
            text = text[:idx] + insertion + text[idx:]
        else:
            text = insertion + text
    elif not has_sweep:
        ci_line_re = re.compile(
            rf"^(\[!\[ci\]\(https://codeberg\.org/{re.escape(owner)}/{re.escape(repo)}/"
            rf"actions/workflows/ci\.yml/badge\.svg\)\]\(https://codeberg\.org/"
            rf"{re.escape(owner)}/{re.escape(repo)}/actions\?workflow=ci\.yml\))$",
            re.MULTILINE,
        )
        sweep_badge = _canonical_badge(owner, repo, "gitleaks-sweep")
        text = ci_line_re.sub(lambda m: m.group(1) + "\n" + sweep_badge, text, count=1)

    return text


PR_BODY_TEMPLATE = (
    "Auto-generated sync from `example-org/ci-workflows` @ **{to_tag}**.\n\n"
    "**Overwrites:**\n"
    "- `.github/workflows/ci.yml`\n"
    "- `.github/workflows/gitleaks-sweep.yml`\n"
    "- `.ci-workflows-version`\n"
    "- `README.md` (stale CI badges → canonical; only if changes detected)\n\n"
    "**Removes** (superseded by the templated workflows):\n"
    "- any non-templated `*.yml` / `*.yaml` in `.github/workflows/` "
    "(except entries listed in the caller's `preserve:` config in `callers.yml`)\n"
    "- any `*.yml` / `*.yaml` in `.forgejo/workflows/`\n\n"
    "Per-caller customization files (`.ruff.toml`, `.gitleaks.toml`, `.markdownlint.json`, etc.) "
    "are preserved and consumed by the tier workflow via tool-native fallback.\n\n"
    "Re-run `sync.py --caller-filter={repo} --version={to_tag}` to force-refresh this PR.\n"
)


def self_ci_gate(client, *, owner: str, repo: str, branch: str, n: int = 1) -> bool:
    """Central repo self-CI must be green before any fanout.

    Returns True iff the most recent N runs on `branch` are `success`.
    """
    return client.all_green(owner, repo, branch, n=n)


def probe_token_scope(client, *, owner: str, repo: str) -> None:
    """Round-trip a dummy secret PUT+DELETE to verify token scope before fanout.

    Raises ForgejoError on failure — caller must let it propagate to abort.
    """
    client.put_secret(owner, repo, PROBE_SECRET_NAME, "probe-value-for-scope-check")
    client.delete_secret(owner, repo, PROBE_SECRET_NAME)


@dataclass(frozen=True)
class RenderedCaller:
    caller: Caller
    target_path: str  # path within the caller repo
    content: str
    template_source: str  # e.g. "templates/python-app/ci.yml"
    marker_content: str  # same marker across both files of a caller
    cron_minute: int | None


def plan_renders(
    *,
    registry: Registry,
    templates_root: Path,
    version: str,
    synced: str,
    source_commit: str,
    caller_filter: str | None = None,
    exclude: set[str] | None = None,
) -> list[RenderedCaller]:
    exclude = exclude or set()
    effective_filter = None if (not caller_filter or caller_filter == "all") else caller_filter
    out: list[RenderedCaller] = []
    for caller in registry.callers:
        if effective_filter and caller.repo != effective_filter:
            continue
        if caller.repo in exclude:
            continue
        cron_minute = cron_minute_for_repo(caller.repo)
        marker = render_marker(
            version=version,
            tier=caller.tier,
            source_commit=source_commit,
            synced=synced,
            synced_by=f"sync.py@{version}",
        )
        for filename in ("ci.yml", "gitleaks-sweep.yml"):
            tpl = templates_root / caller.tier / filename
            if not tpl.exists():
                raise FileNotFoundError(f"missing template: {tpl}")
            content = render_template(
                template_path=tpl,
                templates_root=templates_root,
                caller_entry=caller,
                version=version,
                synced=synced,
                cron_minute=cron_minute if "sweep" in filename else None,
            )
            out.append(
                RenderedCaller(
                    caller=caller,
                    target_path=f"{WORKFLOW_DIR}/{filename}",
                    content=content,
                    template_source=str(tpl.relative_to(templates_root.parent)),
                    marker_content=marker,
                    cron_minute=cron_minute,
                )
            )
    return out


def _ensure_branch(client, owner: str, repo: str, *, branch: str, from_ref: str) -> None:
    try:
        client.create_branch(owner, repo, new_branch=branch, from_ref=from_ref)
    except ForgejoError as e:
        if "409" not in str(e):
            raise


SWEEP_WORKFLOW_DIRS = (".github/workflows", ".forgejo/workflows")


def _sweep_stale_workflows(
    client,
    owner: str,
    repo: str,
    *,
    branch: str,
    commit_msg: str,
    preserve: frozenset[str] = frozenset(),
) -> list[str]:
    """Delete stale workflow files across BOTH `.github/workflows/` and
    `.forgejo/workflows/` on `branch`. Returns the list of deleted full paths.

    Codeberg runs workflows from both dirs — many callers have legacy per-repo CI
    under `.forgejo/workflows/` that templates are meant to replace. Templates
    target `.github/workflows/` exclusively; anything under `.forgejo/workflows/`
    is legacy and always swept. In `.github/workflows/`, only non-templated files
    are swept (templated `ci.yml` + `gitleaks-sweep.yml` are preserved, plus any
    filenames in `preserve` — caller-specific opt-outs from sweep).

    `preserve` only applies to `.github/workflows/` — `.forgejo/workflows/` is
    legacy and never has preserved entries.

    Called after writing the templated files so a failure here doesn't prevent
    the templated workflows from landing. Missing dirs are treated as "nothing
    to sweep" (list_dir returns []).
    """
    deleted: list[str] = []
    for sweep_dir in SWEEP_WORKFLOW_DIRS:
        items = client.list_dir(owner, repo, sweep_dir, ref=branch)
        for item in items:
            if item.get("type") != "file":
                continue
            name = item.get("name") or ""
            if not (name.endswith(".yml") or name.endswith(".yaml")):
                continue
            if sweep_dir == WORKFLOW_DIR and name in TEMPLATED_WORKFLOW_FILES:
                continue
            if sweep_dir == WORKFLOW_DIR and name in preserve:
                continue
            path = f"{sweep_dir}/{name}"
            client.delete_file(
                owner,
                repo,
                path,
                sha=item["sha"],
                message=commit_msg,
                branch=branch,
            )
            deleted.append(path)
    return deleted


def _upsert_file(
    client,
    owner: str,
    repo: str,
    *,
    path: str,
    content: str,
    branch: str,
    commit_msg: str,
) -> None:
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    existing_sha: str | None = None
    try:
        existing = client.get_file(owner, repo, path, ref=branch)
        existing_sha = existing.get("sha")
    except ForgejoError:
        existing_sha = None
    if existing_sha:
        client.put_file(
            owner,
            repo,
            path,
            content_b64=b64,
            message=commit_msg,
            branch=branch,
            sha=existing_sha,
        )
    else:
        client.create_file(owner, repo, path, content_b64=b64, message=commit_msg, branch=branch)


def sync_one_caller(
    client,
    *,
    caller_repo: str,
    rendered: list[RenderedCaller],
    to_tag: str,
) -> dict:
    owner, repo = caller_repo.split("/", 1)
    default = client.get_repo(owner, repo).get("default_branch", "main")
    branch = f"sync/{to_tag}"

    _ensure_branch(client, owner, repo, branch=branch, from_ref=default)

    commit_msg = f"sync: bump to {to_tag}"
    marker = rendered[0].marker_content
    _upsert_file(
        client,
        owner,
        repo,
        path=MARKER_PATH,
        content=marker,
        branch=branch,
        commit_msg=commit_msg,
    )
    for r in rendered:
        _upsert_file(
            client,
            owner,
            repo,
            path=r.target_path,
            content=r.content,
            branch=branch,
            commit_msg=commit_msg,
        )

    # Regenerate README CI badges in the same sync commit. Skip silently if the
    # caller has no README.md — not every repo ships one.
    try:
        readme_obj = client.get_file(owner, repo, README_PATH, ref=branch)
    except ForgejoError:
        readme_obj = None
    if readme_obj is not None:
        current_readme = base64.b64decode(readme_obj["content"]).decode("utf-8")
        new_readme = _regenerate_readme_badges(current_readme, owner, repo)
        if new_readme != current_readme:
            new_b64 = base64.b64encode(new_readme.encode("utf-8")).decode("ascii")
            client.put_file(
                owner,
                repo,
                path=README_PATH,
                content_b64=new_b64,
                message=commit_msg,
                branch=branch,
                sha=readme_obj["sha"],
            )

    # Sweep runs AFTER writes so templated files land even if sweep errors.
    preserve = frozenset(rendered[0].caller.preserve)
    swept = _sweep_stale_workflows(client, owner, repo, branch=branch, commit_msg=commit_msg, preserve=preserve)
    if swept:
        print(f"  {caller_repo}: swept {len(swept)} stale workflow(s): {', '.join(swept)}")

    existing = [p for p in client.list_pulls(owner, repo, state="open") if p.get("head", {}).get("ref") == branch]
    if existing:
        return existing[0]
    return client.create_pull(
        owner,
        repo,
        title=f"[{to_tag}] {repo}: sync templates",
        head=branch,
        base=default,
        body=PR_BODY_TEMPLATE.format(to_tag=to_tag, repo=caller_repo),
    )


def fanout(
    client,
    *,
    by_caller: dict[str, list[RenderedCaller]],
    to_tag: str,
    cooldown_s: int = 0,
) -> list[tuple[str, Exception]]:
    """Run sync_one_caller across every caller in `by_caller`.

    Sleeps `cooldown_s` seconds between successful callers (Codeberg's
    undocumented "5 issues per 5 min" burst limit; recommend 360 for fanouts
    over 5 callers). Sleep is skipped on the last iteration and on the
    failure path — failed calls already consumed cycles, no need to pad.

    Returns list of (repo, exception) tuples for callers that failed.
    """
    failures: list[tuple[str, Exception]] = []
    total = len(by_caller)
    for i, (repo, rendered) in enumerate(by_caller.items()):
        try:
            pr = sync_one_caller(client, caller_repo=repo, rendered=rendered, to_tag=to_tag)
            print(f"  {repo}: PR #{pr['number']} → {pr.get('html_url', '')}")
        except Exception as e:
            failures.append((repo, e))
            continue
        if cooldown_s > 0 and i < total - 1:
            time.sleep(cooldown_s)
    return failures


def soak_check(
    client,
    *,
    caller_repo: str,
    min_runs: int,
    timeout_s: int = 4 * 3600,
    poll_s: int = 60,
) -> int:
    owner, repo = caller_repo.split("/", 1)
    default = client.get_repo(owner, repo).get("default_branch", "main")
    deadline = time.time() + timeout_s
    while True:
        if client.all_green(owner, repo, default, n=min_runs):
            return 0
        if time.time() >= deadline:
            return 1
        time.sleep(poll_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render templates into callers + open PRs.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--caller-filter", default="all")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--version", help="central repo tag, e.g. v1")
    parser.add_argument("--templates-root", default="templates")
    parser.add_argument("--registry", default="callers.yml")
    parser.add_argument("--ignore-self-ci-red", action="store_true")
    parser.add_argument(
        "--rate-limit-cooldown",
        type=int,
        default=0,
        help=(
            "seconds to sleep between callers in the fanout loop (Codeberg's "
            "undocumented 5-issues-per-5-minutes burst limit; recommend 360 "
            "for fanouts over 5 callers, 0 for single-caller syncs)"
        ),
    )
    parser.add_argument("--soak-check", metavar="REPO", help="poll until N green runs then exit 0")
    parser.add_argument("--min-runs", type=int, default=3)
    parser.add_argument("--timeout-s", type=int, default=4 * 3600)
    parser.add_argument("--poll-s", type=int, default=60)
    args = parser.parse_args(argv)

    token = os.environ.get("CODEBERG_TOKEN")
    if not token and not args.dry_run:
        print("ERROR: set CODEBERG_TOKEN (example-org-scope).", file=sys.stderr)
        return 2

    client = None
    if token:
        client = ForgejoClient(base_url="https://codeberg.org", token=token)

    if args.soak_check:
        return soak_check(
            client,
            caller_repo=args.soak_check,
            min_runs=args.min_runs,
            timeout_s=args.timeout_s,
            poll_s=args.poll_s,
        )

    if not args.version:
        print("ERROR: --version required unless --soak-check is given.", file=sys.stderr)
        return 2

    if client and not args.dry_run and not args.ignore_self_ci_red:
        if not self_ci_gate(client, owner="example-org", repo="ci-workflows", branch="main", n=1):
            print(
                "ERROR: central self-CI not green on main — aborting fanout. Pass --ignore-self-ci-red to override.",
                file=sys.stderr,
            )
            return 3

    if client and not args.dry_run:
        try:
            probe_token_scope(client, owner="example-org", repo="ci-workflows")
        except ForgejoError as e:
            print(
                f"ERROR: secrets-API probe failed — token scope insufficient: {e}",
                file=sys.stderr,
            )
            return 4

    from datetime import datetime

    synced = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_commit = os.environ.get("CI_WORKFLOWS_COMMIT", "HEAD")
    registry = load_registry(Path(args.registry))
    plan = plan_renders(
        registry=registry,
        templates_root=Path(args.templates_root),
        version=args.version,
        synced=synced,
        source_commit=source_commit,
        caller_filter=args.caller_filter,
        exclude=set(args.exclude),
    )

    print(f"Planned {len(plan)} file(s) across {len({r.caller.repo for r in plan})} caller(s).")

    if args.dry_run:
        for r in plan:
            print(f"  {r.caller.repo}:{r.target_path} ← {r.template_source}")
        return 0

    by_caller: dict[str, list[RenderedCaller]] = {}
    for r in plan:
        by_caller.setdefault(r.caller.repo, []).append(r)

    failures = fanout(
        client,
        by_caller=by_caller,
        to_tag=args.version,
        cooldown_s=args.rate_limit_cooldown,
    )

    if failures:
        print(f"\n{len(failures)} caller(s) failed during commit phase:", file=sys.stderr)
        for repo, err in failures:
            print(f"  {repo}: {err}", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
