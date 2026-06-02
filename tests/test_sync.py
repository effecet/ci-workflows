import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ci_workflows.forgejo import ForgejoError
from ci_workflows.registry import Caller, load_registry
from ci_workflows.sync import (
    RenderedCaller,
    fanout,
    plan_renders,
    probe_token_scope,
    self_ci_gate,
    soak_check,
    sync_one_caller,
)

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


def test_plan_renders_returns_two_files_per_caller():
    registry = load_registry(FIXTURES / "callers.minimal.yml")
    renders = plan_renders(
        registry=registry,
        templates_root=REPO_ROOT / "templates",
        version="v1",
        synced="2026-04-20T14:30:00Z",
        source_commit="abc1234",
    )
    # 2 callers × 2 files (ci + sweep) = 4
    assert len(renders) == 4
    # ensure every rendered caller has a marker set
    for r in renders:
        assert r.marker_content.startswith("# Auto-generated")


def test_plan_renders_uses_correct_tier_template():
    registry = load_registry(FIXTURES / "callers.minimal.yml")
    renders = plan_renders(
        registry=registry,
        templates_root=REPO_ROOT / "templates",
        version="v1",
        synced="2026-04-20T14:30:00Z",
        source_commit="abc1234",
    )
    by_repo = {}
    for r in renders:
        by_repo.setdefault(r.caller.repo, []).append(r)
    alpha = by_repo["example-org/alpha"]
    assert all("python-app" in r.template_source for r in alpha)
    beta = by_repo["example-org/beta"]
    assert all("docs-static" in r.template_source for r in beta)


def test_plan_renders_stagger_differs_across_callers():
    registry = load_registry(FIXTURES / "callers.minimal.yml")
    renders = plan_renders(
        registry=registry,
        templates_root=REPO_ROOT / "templates",
        version="v1",
        synced="2026-04-20T14:30:00Z",
        source_commit="abc1234",
    )
    sweeps = [r for r in renders if "sweep" in r.target_path]
    cron_minutes = {r.cron_minute for r in sweeps}
    # Relies on sha256(short-name) % 60 producing distinct values for "alpha" and "beta".
    # Update the fixture if a future synthetic name collides on this hash.
    assert len(cron_minutes) == 2  # alpha and beta get different minutes


def test_plan_renders_single_caller_filter():
    registry = load_registry(FIXTURES / "callers.minimal.yml")
    renders = plan_renders(
        registry=registry,
        templates_root=REPO_ROOT / "templates",
        version="v1",
        synced="2026-04-20T14:30:00Z",
        source_commit="abc1234",
        caller_filter="example-org/alpha",
    )
    assert {r.caller.repo for r in renders} == {"example-org/alpha"}


def test_plan_renders_all_filter_includes_every_caller():
    registry = load_registry(FIXTURES / "callers.minimal.yml")
    renders = plan_renders(
        registry=registry,
        templates_root=REPO_ROOT / "templates",
        version="v1",
        synced="2026-04-20T14:30:00Z",
        source_commit="abc1234",
        caller_filter="all",
    )
    assert {r.caller.repo for r in renders} == {"example-org/alpha", "example-org/beta"}


def test_plan_renders_empty_filter_includes_every_caller():
    registry = load_registry(FIXTURES / "callers.minimal.yml")
    renders = plan_renders(
        registry=registry,
        templates_root=REPO_ROOT / "templates",
        version="v1",
        synced="2026-04-20T14:30:00Z",
        source_commit="abc1234",
        caller_filter="",
    )
    assert {r.caller.repo for r in renders} == {"example-org/alpha", "example-org/beta"}


def test_self_ci_gate_passes_when_green():
    client = MagicMock()
    client.all_green.return_value = True
    assert self_ci_gate(client, owner="example-org", repo="ci-workflows", branch="main") is True


def test_self_ci_gate_fails_when_red():
    client = MagicMock()
    client.all_green.return_value = False
    assert self_ci_gate(client, owner="example-org", repo="ci-workflows", branch="main") is False


def test_probe_token_scope_roundtrips_dummy_secret():
    client = MagicMock()
    probe_token_scope(client, owner="example-org", repo="ci-workflows")
    client.put_secret.assert_called_once()
    client.delete_secret.assert_called_once()


def test_probe_token_scope_raises_if_put_fails():
    client = MagicMock()
    client.put_secret.side_effect = ForgejoError("PUT → 403: Forbidden")
    with pytest.raises(ForgejoError):
        probe_token_scope(client, owner="example-org", repo="ci-workflows")


def _fake_rendered(repo: str = "example-org/alpha", *, preserve: tuple[str, ...] = ()) -> list[RenderedCaller]:
    caller = Caller(repo=repo, tier="python-app", pilot=True, preserve=preserve)
    marker = "# Auto-generated\nversion: v1\n"
    return [
        RenderedCaller(
            caller=caller,
            target_path=".github/workflows/ci.yml",
            content="# ci\n",
            template_source="templates/python-app/ci.yml",
            marker_content=marker,
            cron_minute=None,
        ),
        RenderedCaller(
            caller=caller,
            target_path=".github/workflows/gitleaks-sweep.yml",
            content="# sweep\n",
            template_source="templates/python-app/gitleaks-sweep.yml",
            marker_content=marker,
            cron_minute=17,
        ),
    ]


def test_sync_one_caller_force_refreshes_branch_and_opens_pr():
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.side_effect = ForgejoError("404")  # files don't exist yet
    client.list_dir.return_value = []  # no stale workflows
    client.list_pulls.return_value = []
    client.create_pull.return_value = {
        "number": 42,
        "html_url": "https://codeberg.org/example-org/alpha/pulls/42",
    }
    rendered = _fake_rendered("example-org/alpha")
    pr = sync_one_caller(client, caller_repo="example-org/alpha", rendered=rendered, to_tag="v1")
    client.create_branch.assert_called_once()
    # New files use POST (create_file); existing files use PUT with sha.
    # Here all 3 files are new (get_file 404s), so 3 create_file, 0 put_file.
    assert client.create_file.call_count == 3
    assert client.put_file.call_count == 0
    client.create_pull.assert_called_once()
    assert pr["number"] == 42


def test_pr_title_includes_caller_short_name():
    """PR title must encode the caller's short name to defeat Codeberg's
    similar-titled-issues anti-spam (empirically tripped on v2.0.3 fanout,
    blocked 12 of 14 PRs). Title format: f"[{to_tag}] {short_name}: sync templates".
    """
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.side_effect = ForgejoError("404")
    client.list_dir.return_value = []
    client.list_pulls.return_value = []
    client.create_pull.return_value = {"number": 1, "html_url": "x"}
    rendered = _fake_rendered("example-org/foo-bar")
    sync_one_caller(client, caller_repo="example-org/foo-bar", rendered=rendered, to_tag="v2.0.4")
    title = client.create_pull.call_args.kwargs["title"]
    assert title == "[v2.0.4] foo-bar: sync templates", f"expected per-caller title, got {title!r}"


def test_sync_one_caller_reuses_existing_pr():
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}

    def get_file(_owner, _repo, path, ref="main"):
        # README is unrelated to this test's focus (PR reuse); mark it as 404.
        if path == "README.md":
            raise ForgejoError("404")
        return {"sha": "old-sha"}

    client.get_file.side_effect = get_file
    client.list_dir.return_value = []  # no stale workflows
    client.list_pulls.return_value = [{"number": 9, "head": {"ref": "sync/v1"}, "html_url": "..."}]
    rendered = _fake_rendered("example-org/alpha")
    pr = sync_one_caller(client, caller_repo="example-org/alpha", rendered=rendered, to_tag="v1")
    # All 3 files exist (get_file returns sha), so all 3 writes route to PUT, not POST.
    assert client.put_file.call_count == 3
    assert client.create_file.call_count == 0
    client.create_pull.assert_not_called()
    assert pr["number"] == 9


def test_sync_one_caller_skips_create_branch_if_exists():
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.create_branch.side_effect = ForgejoError("409 branch already exists")
    client.get_file.side_effect = ForgejoError("404")
    client.list_dir.return_value = []  # no stale workflows
    client.list_pulls.return_value = []
    client.create_pull.return_value = {"number": 1, "html_url": "..."}
    rendered = _fake_rendered("example-org/alpha")
    sync_one_caller(client, caller_repo="example-org/alpha", rendered=rendered, to_tag="v1")
    # Branch 409 swallowed; all 3 files are new (get_file 404s), so 3 create_file.
    assert client.create_file.call_count == 3
    assert client.put_file.call_count == 0


def test_sync_one_caller_sweeps_stale_workflows():
    """After writing templated workflows, stale files in BOTH .github/workflows/ and
    .forgejo/workflows/ should be deleted. In .github/workflows/ the templated names
    (ci.yml + gitleaks-sweep.yml) are preserved; in .forgejo/workflows/ all yml
    files are swept since templates target only .github/workflows/.
    """
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "master"}
    client.get_file.side_effect = ForgejoError("404")

    def list_dir_mock(_owner, _repo, path, *, ref):
        if path == ".github/workflows":
            return [
                {"name": "ci.yml", "type": "file", "sha": "sha-ci"},  # templated → keep
                {
                    "name": "gitleaks-sweep.yml",
                    "type": "file",
                    "sha": "sha-sweep",
                },  # templated → keep
                {
                    "name": "legacy-deploy.yml",
                    "type": "file",
                    "sha": "sha-legacy",
                },  # non-templated → delete
                {
                    "name": "README.md",
                    "type": "file",
                    "sha": "sha-readme",
                },  # non-yml → keep
            ]
        if path == ".forgejo/workflows":
            return [
                {"name": "ruff.yml", "type": "file", "sha": "sha-ruff"},
                {"name": "pytest.yml", "type": "file", "sha": "sha-pytest"},
                {"name": "gitleaks.yml", "type": "file", "sha": "sha-gitleaks"},
            ]
        return []

    client.list_dir.side_effect = list_dir_mock
    client.list_pulls.return_value = []
    client.create_pull.return_value = {"number": 7, "html_url": "..."}
    rendered = _fake_rendered("example-org/alpha")
    sync_one_caller(client, caller_repo="example-org/alpha", rendered=rendered, to_tag="v1")
    # 4 deletions: 1 legacy in .github/workflows/ + 3 in .forgejo/workflows/
    assert client.delete_file.call_count == 4
    deleted_paths = [c.args[2] for c in client.delete_file.call_args_list]
    assert set(deleted_paths) == {
        ".github/workflows/legacy-deploy.yml",
        ".forgejo/workflows/ruff.yml",
        ".forgejo/workflows/pytest.yml",
        ".forgejo/workflows/gitleaks.yml",
    }


def _fanout_client_all_success():
    """Build a MagicMock client where every sync_one_caller call would succeed."""
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.side_effect = ForgejoError("404")
    client.list_dir.return_value = []
    client.list_pulls.return_value = []
    # create_pull returns a sequence of PRs (one per caller).
    client.create_pull.side_effect = [{"number": i, "html_url": f"x{i}"} for i in range(1, 50)]
    return client


def test_rate_limit_cooldown_sleeps_between_callers(monkeypatch):
    """--rate-limit-cooldown=N sleeps N seconds AFTER each caller except the last.
    3 callers, all success → exactly 2 sleeps of N seconds.
    """
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    client = _fanout_client_all_success()
    by_caller = {
        "example-org/a": _fake_rendered("example-org/a"),
        "example-org/b": _fake_rendered("example-org/b"),
        "example-org/c": _fake_rendered("example-org/c"),
    }
    failures = fanout(client, by_caller=by_caller, to_tag="v1", cooldown_s=360)
    assert failures == []
    assert sleep_calls == [360, 360], f"expected 2 sleeps of 360s between 3 callers, got {sleep_calls}"


def test_rate_limit_cooldown_default_zero_no_sleep(monkeypatch):
    """cooldown_s=0 (default) means no sleep regardless of caller count."""
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    client = _fanout_client_all_success()
    by_caller = {
        "example-org/a": _fake_rendered("example-org/a"),
        "example-org/b": _fake_rendered("example-org/b"),
    }
    fanout(client, by_caller=by_caller, to_tag="v1", cooldown_s=0)
    assert sleep_calls == [], f"expected no sleeps when cooldown_s=0, got {sleep_calls}"


def test_rate_limit_cooldown_single_caller_no_sleep(monkeypatch):
    """Single-caller fanout (the dominant path: --caller-filter=foo, `make pilot`)
    must NOT sleep — i==0 is also the last iteration. Locks the boundary case."""
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    client = _fanout_client_all_success()
    by_caller = {"example-org/only": _fake_rendered("example-org/only")}
    fanout(client, by_caller=by_caller, to_tag="v1", cooldown_s=360)
    assert sleep_calls == [], f"single-caller fanout must not sleep, got {sleep_calls}"


def test_rate_limit_cooldown_negative_value_no_sleep(monkeypatch):
    """argparse type=int accepts negatives; the > 0 guard must drop them silently."""
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    client = _fanout_client_all_success()
    by_caller = {
        "example-org/a": _fake_rendered("example-org/a"),
        "example-org/b": _fake_rendered("example-org/b"),
    }
    fanout(client, by_caller=by_caller, to_tag="v1", cooldown_s=-1)
    assert sleep_calls == [], f"negative cooldown must be treated as 0, got {sleep_calls}"


def test_rate_limit_cooldown_mixed_success_failure(monkeypatch):
    """Success → failure → success. Sleep after the first success (not last,
    not failed); no sleep after the failure (failure-path skip); no sleep
    after the third (last). Locks the "sleep after success even when next
    caller will fail" semantic."""
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.side_effect = ForgejoError("404")
    client.list_dir.return_value = []
    client.list_pulls.return_value = []
    client.create_pull.side_effect = [
        {"number": 1, "html_url": "x"},
        ForgejoError("simulated failure on caller b"),
        {"number": 3, "html_url": "z"},
    ]
    by_caller = {
        "example-org/a": _fake_rendered("example-org/a"),
        "example-org/b": _fake_rendered("example-org/b"),
        "example-org/c": _fake_rendered("example-org/c"),
    }
    failures = fanout(client, by_caller=by_caller, to_tag="v1", cooldown_s=360)
    assert sleep_calls == [360], (
        f"expected 1 sleep after caller a (success, not last, next will fail); got {sleep_calls}"
    )
    assert len(failures) == 1
    assert failures[0][0] == "example-org/b"


def test_rate_limit_cooldown_skips_sleep_on_failure(monkeypatch):
    """If sync_one_caller raises for a caller, NO sleep follows that caller —
    the failure already consumed cycles; padding adds no value. The next caller
    runs immediately."""
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.side_effect = ForgejoError("404")
    client.list_dir.return_value = []
    client.list_pulls.return_value = []
    # First create_pull raises, second succeeds.
    client.create_pull.side_effect = [
        ForgejoError("simulated failure on caller a"),
        {"number": 2, "html_url": "y"},
    ]
    by_caller = {
        "example-org/a": _fake_rendered("example-org/a"),
        "example-org/b": _fake_rendered("example-org/b"),
    }
    failures = fanout(client, by_caller=by_caller, to_tag="v1", cooldown_s=360)
    # caller a failed → no sleep after a.
    # caller b succeeded but is last → no sleep after b.
    assert sleep_calls == [], f"failure-path must skip sleep, got {sleep_calls}"
    assert len(failures) == 1
    assert failures[0][0] == "example-org/a"


def test_preserve_keeps_listed_workflows():
    """A caller with preserve=("pi-smoke.yml",) → sweep deletes legacy-deploy.yml
    but leaves pi-smoke.yml alone. Templated ci.yml + gitleaks-sweep.yml stay too.
    """
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.side_effect = ForgejoError("404")
    client.list_pulls.return_value = []
    client.create_pull.return_value = {"number": 1, "html_url": "x"}

    def list_dir_mock(_owner, _repo, path, *, ref):
        if path == ".github/workflows":
            return [
                {"name": "ci.yml", "type": "file", "sha": "sha-ci"},  # templated
                {"name": "gitleaks-sweep.yml", "type": "file", "sha": "sha-sweep"},  # templated
                {"name": "pi-smoke.yml", "type": "file", "sha": "sha-pi"},  # preserved
                {"name": "legacy-deploy.yml", "type": "file", "sha": "sha-legacy"},  # deleted
            ]
        return []

    client.list_dir.side_effect = list_dir_mock
    rendered = _fake_rendered("example-org/alpha", preserve=("pi-smoke.yml",))
    sync_one_caller(client, caller_repo="example-org/alpha", rendered=rendered, to_tag="v1")

    deleted_paths = [c.args[2] for c in client.delete_file.call_args_list]
    assert deleted_paths == [".github/workflows/legacy-deploy.yml"], (
        f"expected only legacy-deploy.yml deleted, got {deleted_paths}"
    )


def test_preserve_only_applies_to_github_workflows_dir():
    """preserve list only protects .github/workflows/ — files under
    .forgejo/workflows/ are ALWAYS swept (always legacy), even if the
    filename matches a preserve entry."""
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.side_effect = ForgejoError("404")
    client.list_pulls.return_value = []
    client.create_pull.return_value = {"number": 1, "html_url": "x"}

    def list_dir_mock(_owner, _repo, path, *, ref):
        if path == ".github/workflows":
            return [
                {"name": "ci.yml", "type": "file", "sha": "sha-ci"},
                {"name": "gitleaks-sweep.yml", "type": "file", "sha": "sha-sweep"},
                {"name": "pi-smoke.yml", "type": "file", "sha": "sha-pi"},  # preserved
            ]
        if path == ".forgejo/workflows":
            return [
                {"name": "pi-smoke.yml", "type": "file", "sha": "sha-pi-legacy"},  # SWEPT
                {"name": "old-build.yml", "type": "file", "sha": "sha-build"},
            ]
        return []

    client.list_dir.side_effect = list_dir_mock
    rendered = _fake_rendered("example-org/alpha", preserve=("pi-smoke.yml",))
    sync_one_caller(client, caller_repo="example-org/alpha", rendered=rendered, to_tag="v1")

    deleted_paths = {c.args[2] for c in client.delete_file.call_args_list}
    # .github/workflows/pi-smoke.yml is preserved.
    # .forgejo/workflows/pi-smoke.yml is swept (legacy dir, unconditional).
    assert deleted_paths == {
        ".forgejo/workflows/pi-smoke.yml",
        ".forgejo/workflows/old-build.yml",
    }, f"expected only .forgejo files swept, got {deleted_paths}"


def test_soak_check_exits_0_when_runs_already_green(monkeypatch):
    client = MagicMock()
    client.all_green.return_value = True
    client.get_repo.return_value = {"default_branch": "main"}
    monkeypatch.setattr("time.sleep", lambda _: None)
    rc = soak_check(client, caller_repo="example-org/example-app", min_runs=3, timeout_s=10, poll_s=1)
    assert rc == 0


def test_soak_check_polls_then_exits_on_green(monkeypatch):
    client = MagicMock()
    client.all_green.side_effect = [False, False, True]
    client.get_repo.return_value = {"default_branch": "main"}
    monkeypatch.setattr("time.sleep", lambda _: None)
    rc = soak_check(client, caller_repo="example-org/example-app", min_runs=3, timeout_s=10, poll_s=1)
    assert rc == 0
    assert client.all_green.call_count == 3


def test_soak_check_times_out_returns_1(monkeypatch):
    client = MagicMock()
    client.all_green.return_value = False
    client.get_repo.return_value = {"default_branch": "main"}
    # Monkeypatch time.time to advance past deadline after first check.
    times = iter([1000.0, 1000.0, 1001.5])
    monkeypatch.setattr("time.time", lambda: next(times))
    monkeypatch.setattr("time.sleep", lambda _: None)
    rc = soak_check(client, caller_repo="example-org/example-app", min_runs=3, timeout_s=1, poll_s=1)
    assert rc == 1


def _readme_get_file_side_effect(readme_text: str, *, sha: str = "readme-sha"):
    """MagicMock side_effect: README.md returns content; everything else 404s."""

    def get_file(_owner, _repo, path, ref="main"):
        if path == "README.md":
            return {
                "content": base64.b64encode(readme_text.encode()).decode(),
                "sha": sha,
            }
        raise ForgejoError("404")

    return get_file


def test_sync_one_caller_regenerates_stale_readme():
    stale_readme = (
        "# example-app\n\n"
        "[![ruff](https://codeberg.org/example-org/example-app/actions/workflows/ruff.yml/badge.svg)]"
        "(https://codeberg.org/example-org/example-app/actions?workflow=ruff.yml)\n\n"
        "A project.\n"
    )
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.side_effect = _readme_get_file_side_effect(stale_readme)
    client.list_pulls.return_value = []
    client.list_dir.return_value = []
    client.create_pull.return_value = {
        "number": 1,
        "html_url": "https://example.test/pr/1",
    }

    rendered = _fake_rendered("example-org/example-app")
    sync_one_caller(client, caller_repo="example-org/example-app", rendered=rendered, to_tag="v2")

    readme_calls = [c for c in client.put_file.call_args_list if c.kwargs.get("path") == "README.md"]
    assert readme_calls, f"expected a put_file call targeting README.md, got {client.put_file.call_args_list}"
    written_b64 = readme_calls[0].kwargs.get("content_b64")
    written = base64.b64decode(written_b64).decode()
    assert "ci.yml/badge.svg" in written
    assert "gitleaks-sweep.yml/badge.svg" in written
    assert "ruff.yml/badge" not in written


def test_sync_one_caller_skips_readme_write_when_canonical():
    canonical = (
        "# example-app\n\n"
        "[![ci](https://codeberg.org/example-org/example-app/actions/workflows/ci.yml/badge.svg)]"
        "(https://codeberg.org/example-org/example-app/actions?workflow=ci.yml)\n"
        "[![gitleaks-sweep](https://codeberg.org/example-org/example-app/actions/workflows/gitleaks-sweep.yml/badge.svg)]"
        "(https://codeberg.org/example-org/example-app/actions?workflow=gitleaks-sweep.yml)\n\n"
        "A project.\n"
    )
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.side_effect = _readme_get_file_side_effect(canonical)
    client.list_pulls.return_value = []
    client.list_dir.return_value = []
    client.create_pull.return_value = {"number": 1, "html_url": "x"}

    sync_one_caller(
        client,
        caller_repo="example-org/example-app",
        rendered=_fake_rendered("example-org/example-app"),
        to_tag="v2",
    )
    readme_calls = [c for c in client.put_file.call_args_list if c.kwargs.get("path") == "README.md"]
    assert not readme_calls, f"expected no README.md write for idempotent canonical README; got {readme_calls}"


def test_sync_one_caller_handles_missing_readme():
    """Caller has no README.md — sync should silently skip the regen step."""
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.side_effect = ForgejoError("404")  # all 404, including README
    client.list_pulls.return_value = []
    client.list_dir.return_value = []
    client.create_pull.return_value = {"number": 1, "html_url": "x"}

    sync_one_caller(
        client,
        caller_repo="example-org/no-readme",
        rendered=_fake_rendered("example-org/no-readme"),
        to_tag="v2",
    )
    readme_calls = [c for c in client.put_file.call_args_list if c.kwargs.get("path") == "README.md"]
    assert not readme_calls, "README write must not happen if README.md doesn't exist"
