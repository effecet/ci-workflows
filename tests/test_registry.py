from pathlib import Path

import pytest

from ci_workflows import registry
from ci_workflows.registry import (
    DEFAULT_RUNS_ON,
    TIER_DEFAULTS,
    TIER_JOBS,
    Caller,
    RegistryError,
    load_registry,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_registry_minimal_parses_callers():
    registry = load_registry(FIXTURES / "callers.minimal.yml")
    assert registry.version == 1
    assert len(registry.callers) == 2
    assert registry.callers[0] == Caller(repo="example-org/alpha", tier="python-app", pilot=True)
    assert registry.callers[1] == Caller(repo="example-org/beta", tier="docs-static", pilot=False)


def test_pilot_accessor_returns_exactly_one():
    registry = load_registry(FIXTURES / "callers.minimal.yml")
    assert registry.pilot().repo == "example-org/alpha"


def test_load_registry_rejects_multiple_pilots():
    with pytest.raises(RegistryError, match="exactly one pilot"):
        load_registry(FIXTURES / "callers.multi-pilot.yml")


def test_load_registry_rejects_unknown_tier(tmp_path):
    f = tmp_path / "bad.yml"
    f.write_text("version: 1\ncallers:\n  - repo: example-org/x\n    tier: go-app\n")
    with pytest.raises(RegistryError, match="unknown tier"):
        load_registry(f)


def test_load_registry_rejects_missing_repo(tmp_path):
    f = tmp_path / "bad.yml"
    f.write_text("version: 1\ncallers:\n  - tier: python-app\n    pilot: true\n")
    with pytest.raises(RegistryError, match="missing 'repo' field"):
        load_registry(f)


def test_filter_by_tier():
    registry = load_registry(FIXTURES / "callers.minimal.yml")
    assert [c.repo for c in registry.by_tier("python-app")] == ["example-org/alpha"]
    assert [c.repo for c in registry.by_tier("docs-static")] == ["example-org/beta"]


def test_caller_defaults_retry_setup_true():
    c = Caller(repo="example-org/x", tier="python-app")
    assert c.retry_setup is True


def test_caller_accepts_explicit_retry_setup_false():
    c = Caller(repo="example-org/x", tier="python-app", retry_setup=False)
    assert c.retry_setup is False


def test_load_registry_accepts_retry_setup_true(tmp_path):
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/a\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    retry_setup: true\n"
        "  - repo: example-org/b\n"
        "    tier: python-app\n"
        "    retry_setup: false\n"
    )
    reg = load_registry(p)
    assert reg.callers[0].retry_setup is True
    assert reg.callers[1].retry_setup is False


def test_load_registry_defaults_retry_setup_true_when_omitted(tmp_path):
    p = tmp_path / "callers.yml"
    p.write_text("version: 1\ncallers:\n  - repo: example-org/a\n    tier: python-app\n    pilot: true\n")
    reg = load_registry(p)
    assert reg.callers[0].retry_setup is True


def test_load_registry_rejects_non_bool_retry_setup(tmp_path):
    p = tmp_path / "callers.yml"
    p.write_text(
        'version: 1\ncallers:\n  - repo: example-org/a\n    tier: python-app\n    pilot: true\n    retry_setup: "yes"\n'
    )
    with pytest.raises(RegistryError, match="retry_setup"):
        load_registry(p)


def test_runs_on_uses_tier_defaults_when_no_override():
    """Every tier-default job runs on arm64-runner: no job defaults to a
    hosted `codeberg-*` label (v3.3.0: hosted runners have their own usage policy).
    """
    py = Caller(repo="example-org/x", tier="python-app")
    assert py.runs_on_for("lint-test") == "arm64-runner"
    assert py.runs_on_for("gitleaks-fast") == "arm64-runner"
    assert py.runs_on_for("sweep") == "arm64-runner"

    docs = Caller(repo="example-org/y", tier="docs-static")
    assert docs.runs_on_for("lint-docs") == "arm64-runner"
    assert docs.runs_on_for("gitleaks-fast") == "arm64-runner"
    assert docs.runs_on_for("sweep") == "arm64-runner"


def test_runs_on_falls_back_to_global_default_for_unknown_job():
    """If a job isn't in TIER_DEFAULTS (e.g. a new job added but tier-default
    not yet wired), DEFAULT_RUNS_ON applies."""
    c = Caller(repo="example-org/x", tier="python-app")
    assert c.runs_on_for("nonexistent-job") == "arm64-runner"


def test_runs_on_returns_override_when_set():
    """Explicit override in callers.yml beats tier-default and global default."""
    c = Caller(
        repo="example-org/x",
        tier="python-app",
        runs_on=(("gitleaks-fast", "codeberg-medium"), ("lint-test", "codeberg-medium")),
    )
    # Explicit overrides win
    assert c.runs_on_for("lint-test") == "codeberg-medium"
    assert c.runs_on_for("gitleaks-fast") == "codeberg-medium"
    # No override on sweep → tier-default applies (arm64-runner for python-app sweep)
    assert c.runs_on_for("sweep") == "arm64-runner"


def test_load_registry_parses_runs_on(tmp_path):
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/heavy\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    runs_on:\n"
        "      lint-test: codeberg-medium\n"
        "      gitleaks-fast: arm64-runner\n"
    )
    reg = load_registry(p)
    c = reg.callers[0]
    assert c.runs_on_for("lint-test") == "codeberg-medium"
    assert c.runs_on_for("gitleaks-fast") == "arm64-runner"
    assert c.runs_on_for("sweep") == "arm64-runner"


def test_load_registry_rejects_unknown_runs_on_job(tmp_path):
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/x\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    runs_on:\n"
        "      lint_test: codeberg-medium\n"  # underscore typo, should be hyphen
    )
    with pytest.raises(RegistryError, match="unknown job 'lint_test'"):
        load_registry(p)


def test_load_registry_rejects_docs_static_runs_on_lint_test(tmp_path):
    # lint-test is python-app only; docs-static has lint-docs
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/a\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "  - repo: example-org/b\n"
        "    tier: docs-static\n"
        "    runs_on:\n"
        "      lint-test: codeberg-medium\n"  # wrong job key for docs-static
    )
    with pytest.raises(RegistryError, match="unknown job 'lint-test'"):
        load_registry(p)


def test_load_registry_rejects_non_string_runs_on_label(tmp_path):
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/x\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    runs_on:\n"
        "      lint-test: 42\n"
    )
    with pytest.raises(RegistryError, match="must be a non-empty string"):
        load_registry(p)


def test_load_registry_rejects_non_mapping_runs_on(tmp_path):
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/x\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    runs_on: codeberg-medium\n"  # should be a mapping, not a string
    )
    with pytest.raises(RegistryError, match="must be a mapping"):
        load_registry(p)


def test_preserve_defaults_to_empty_tuple():
    """Caller without preserve set has empty tuple — sweep deletes everything non-templated."""
    c = Caller(repo="example-org/x", tier="python-app")
    assert c.preserve == ()


def test_load_registry_parses_preserve_list(tmp_path):
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/x\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    preserve:\n"
        "      - smoke.yml\n"
        "      - internal-deploy.yaml\n"
    )
    reg = load_registry(p)
    # Stored as sorted tuple for hashability.
    assert reg.callers[0].preserve == ("internal-deploy.yaml", "smoke.yml")


def test_load_registry_rejects_non_list_preserve(tmp_path):
    """preserve must be a YAML list, not a scalar string."""
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/x\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    preserve: smoke.yml\n"  # scalar, not list
    )
    with pytest.raises(RegistryError, match="preserve.*must be a list"):
        load_registry(p)


def test_load_registry_rejects_non_string_preserve_entry(tmp_path):
    """Each preserve entry must be a string."""
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\ncallers:\n  - repo: example-org/x\n    tier: python-app\n"
        "    pilot: true\n    preserve:\n      - 42\n"
    )
    with pytest.raises(RegistryError, match="preserve entry.*must be a non-empty string"):
        load_registry(p)


def test_load_registry_rejects_preserve_with_path_separator(tmp_path):
    """preserve entries must be plain filenames — no directory components."""
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/x\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    preserve:\n"
        "      - subdir/foo.yml\n"
    )
    with pytest.raises(RegistryError, match="preserve entry.*path separators"):
        load_registry(p)


def test_load_registry_rejects_preserve_non_yml_extension(tmp_path):
    """preserve entries must end in .yml or .yaml — workflows are YAML."""
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/x\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    preserve:\n"
        "      - foo.txt\n"
    )
    with pytest.raises(RegistryError, match="preserve entry.*\\.yml or \\.yaml"):
        load_registry(p)


def test_load_registry_rejects_preserve_templated_name(tmp_path):
    """preserve cannot opt-out templated filenames — the template rewrite
    overwrites the file regardless of preserve. Reject at load time to
    prevent silent overwrite-after-preserve confusion."""
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/x\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    preserve:\n"
        "      - ci.yml\n"
    )
    with pytest.raises(RegistryError, match="preserve entry.*is a templated workflow name"):
        load_registry(p)


def test_load_registry_handles_null_preserve(tmp_path):
    """`preserve:` with no value (YAML null) is equivalent to omitting it."""
    p = tmp_path / "callers.yml"
    p.write_text(
        "version: 1\n"
        "callers:\n"
        "  - repo: example-org/x\n"
        "    tier: python-app\n"
        "    pilot: true\n"
        "    preserve:\n"  # YAML null — must not crash
    )
    reg = load_registry(p)
    assert reg.callers[0].preserve == ()


def test_tier_default_wins_over_global_fallback(monkeypatch):
    """TIER_DEFAULTS and DEFAULT_RUNS_ON are both arm64-runner, so a test on real
    values cannot tell which one answered. A sentinel fallback can."""
    monkeypatch.setattr(registry, "DEFAULT_RUNS_ON", "sentinel-fallback")
    c = Caller(repo="example-org/x", tier="docs-static")
    assert c.runs_on_for("lint-docs") == TIER_DEFAULTS["docs-static"]["lint-docs"]
    assert c.runs_on_for("nonexistent-job") == "sentinel-fallback"


def test_every_tier_job_has_a_tier_default():
    """A job missing from TIER_DEFAULTS silently falls through to DEFAULT_RUNS_ON."""
    for tier, jobs in TIER_JOBS.items():
        assert set(TIER_DEFAULTS[tier]) == set(jobs), tier


def test_no_default_is_a_codeberg_hosted_label():
    """Hosted `codeberg-*` runners are for public FLOSS projects only
    under the host's usage policy, which a template can't verify, so a hosted
    label must be an explicit per-caller opt-in, never a default."""
    labels = [DEFAULT_RUNS_ON] + [v for jobs in TIER_DEFAULTS.values() for v in jobs.values()]
    assert [label for label in labels if label.startswith("codeberg-")] == []
