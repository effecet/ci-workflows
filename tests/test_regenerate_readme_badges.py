from pathlib import Path

from ci_workflows.sync import _regenerate_readme_badges

FIXTURES = Path(__file__).parent / "fixtures"
OWNER = "example-org"
REPO = "example-app"


def _canonical_block() -> str:
    return (
        f"[![ci](https://codeberg.org/{OWNER}/{REPO}/actions/workflows/ci.yml/badge.svg)]"
        f"(https://codeberg.org/{OWNER}/{REPO}/actions?workflow=ci.yml)\n"
        f"[![gitleaks-sweep](https://codeberg.org/{OWNER}/{REPO}/actions/workflows/gitleaks-sweep.yml/badge.svg)]"
        f"(https://codeberg.org/{OWNER}/{REPO}/actions?workflow=gitleaks-sweep.yml)\n"
    )


def test_stale_only_rewritten_to_canonical():
    src = (FIXTURES / "readme_stale_only.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    assert _canonical_block() in out
    assert "ruff.yml/badge" not in out
    assert "pytest.yml/badge" not in out


def test_canonical_is_idempotent():
    src = (FIXTURES / "readme_canonical.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    assert out == src


def test_no_badges_inserts_canonical_after_h1():
    src = (FIXTURES / "readme_no_badges.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    lines = out.splitlines(keepends=True)
    assert lines[0].startswith("# ")
    assert _canonical_block().rstrip("\n") in out


def test_no_h1_inserts_at_top():
    src = (FIXTURES / "readme_no_h1.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    assert out.startswith(_canonical_block())


def test_ci_only_adds_sweep_badge():
    src = (FIXTURES / "readme_ci_only.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    assert "ci.yml/badge.svg" in out
    assert "gitleaks-sweep.yml/badge.svg" in out
    # sweep must land on the line immediately after ci (canonical adjacency)
    ci_idx = out.index("ci.yml/badge.svg")
    sweep_idx = out.index("gitleaks-sweep.yml/badge.svg")
    between = out[ci_idx:sweep_idx]
    assert between.count("\n") == 1, (
        f"sweep should be on next line after ci; got {between.count(chr(10))} newlines between"
    )


def test_unknown_workflow_preserved():
    src = (FIXTURES / "readme_unknown_workflow.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    assert "deploy.yml/badge.svg" in out
    assert "ci.yml/badge.svg" in out


def test_mixed_stale_and_non_ci_only_touches_ci():
    src = (FIXTURES / "readme_mixed_stale_and_non_ci.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    assert "License-MIT" in out
    assert "python-3.12-blue" in out
    assert "ruff.yml/badge" not in out
    assert "ci.yml/badge.svg" in out
    assert "gitleaks-sweep.yml/badge.svg" in out


def test_duplicate_stale_collapses_to_single_canonical():
    src = (FIXTURES / "readme_duplicate_stale.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    ci_count = out.count("ci.yml/badge.svg")
    assert ci_count == 1, f"expected exactly one ci.yml badge, got {ci_count}"


def test_foreign_repo_ci_badge_does_not_block_canonical_insert():
    # Regression: a README with a CI badge pointing at OTHER owner/repo (e.g. an
    # upstream lib's status badge) must not be mistaken for this caller's own
    # canonical block — the canonical block must still be inserted.
    src = (FIXTURES / "readme_foreign_repo_ci_badge.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    assert _canonical_block() in out
    assert "some-other-org/some-lib" in out  # foreign badge preserved


def test_h1_without_blank_line_inserts_after_h1_not_at_top():
    # Regression: an H1 not followed by a blank line should still anchor the
    # canonical-block insertion AFTER it, not push it to the top of the file.
    src = (FIXTURES / "readme_h1_no_blank_line.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    assert out.startswith("# example-app"), f"H1 must remain at top; got first line: {out.splitlines()[0]!r}"
    assert _canonical_block().rstrip("\n") in out


def test_sweep_only_does_not_duplicate_sweep_badge():
    # Edge case: a caller has a stray sweep badge but no ci badge. Inserting
    # the canonical block must NOT produce two sweep badges in the output.
    src = (FIXTURES / "readme_sweep_only.md").read_text()
    out = _regenerate_readme_badges(src, OWNER, REPO)
    sweep_count = out.count("gitleaks-sweep.yml/badge.svg")
    assert sweep_count == 1, f"expected exactly one sweep badge, got {sweep_count}"
    assert "ci.yml/badge.svg" in out  # ci badge still inserted
