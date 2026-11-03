from pathlib import Path

SNIPPET = Path(__file__).parent.parent / "templates" / "_snippets" / "install_deps_uv.yml"


def test_install_snippet_is_lock_aware():
    body = SNIPPET.read_text()
    # Lock-aware branch present and gated on uv.lock presence.
    assert "[ -f uv.lock ]" in body, "install must branch on uv.lock presence"
    assert "uv sync --locked --all-extras --dev" in body, (
        "locked path must use --locked (fail-loud on stale lock) with all extras + dev group"
    )


def test_install_snippet_preserves_unlocked_fallback():
    body = SNIPPET.read_text()
    # Lock-less repos keep the existing unlocked editable install.
    assert "uv pip install -e '.[dev]'" in body
    assert "requirements.txt" in body


def test_install_snippet_installs_tooling_after_branch():
    body = SNIPPET.read_text()
    # Tooling install must appear AFTER the uv.lock branch so `uv sync`'s
    # exact-match prune cannot strip it.
    branch_idx = body.index("[ -f uv.lock ]")
    tooling_idx = body.index("uv pip install 'ruff>=0.15,<0.16' pytest pytest-xdist")
    assert tooling_idx > branch_idx, "tooling install must come after the lock branch"


def test_install_snippet_pins_ruff_below_0_16():
    """ruff must stay pinned — an unpinned ruff breaks green callers on release.

    0.16.0 turned on Markdown formatting by default AND expanded the default
    lint rule set (same files, no config: 0.15.22 clean, 0.16.1 → 32 errors),
    taking out callers that had changed nothing. Bumping this ceiling is a
    deliberate migration, so this test fails loudly if the pin is dropped.
    """
    body = SNIPPET.read_text()
    assert "'ruff>=0.15,<0.16'" in body, (
        "ruff must be pinned <0.16 in the tooling install — an unpinned ruff "
        "lets a new release break callers that changed nothing"
    )
    assert "uv pip install ruff " not in body, "found an unpinned `uv pip install ruff` — the pin was dropped"
