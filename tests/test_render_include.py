from pathlib import Path

import pytest
from ci_workflows.registry import Caller
from ci_workflows.render import RenderError, _resolve_includes

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
SNIPPETS_ROOT = FIXTURES  # so {{include: snippets/foo.yml}} resolves under fixtures/


def _caller(retry_setup: bool = True) -> Caller:
    return Caller(repo="example-org/x", tier="python-app", pilot=False, retry_setup=retry_setup)


def test_unconditional_include_expands_with_indent_preserved():
    body = "jobs:\n  job1:\n    steps:\n      - uses: actions/checkout@v4\n      {{include: snippets/hello.yml}}\n"
    out = _resolve_includes(body, SNIPPETS_ROOT, _caller())
    assert "- uses: actions/checkout@v4" in out
    assert "      - name: Hello" in out
    assert "        run: echo hi" in out
    assert "{{include:" not in out


def test_unconditional_include_at_zero_indent():
    body = "{{include: snippets/hello.yml}}\n"
    out = _resolve_includes(body, SNIPPETS_ROOT, _caller())
    assert out == "- name: Hello\n  run: echo hi\n"


def test_missing_snippet_file_raises():
    body = "{{include: snippets/does_not_exist.yml}}\n"
    with pytest.raises(RenderError, match="snippet not found"):
        _resolve_includes(body, SNIPPETS_ROOT, _caller())


def test_nested_include_raises():
    body = "{{include: snippets/nested.yml}}\n"
    with pytest.raises(RenderError, match="nested include"):
        _resolve_includes(body, SNIPPETS_ROOT, _caller())


def test_conditional_include_expands_when_flag_true():
    body = "steps:\n  {{include_if: retry_setup, snippets/hello.yml}}\n"
    out = _resolve_includes(body, SNIPPETS_ROOT, _caller(retry_setup=True))
    assert "- name: Hello" in out
    assert "{{include_if:" not in out


def test_conditional_include_deletes_line_when_flag_false():
    body = (
        "steps:\n"
        "  - uses: actions/checkout@v4\n"
        "  {{include_if: retry_setup, snippets/hello.yml}}\n"
        "  - name: After\n"
        "    run: echo after\n"
    )
    out = _resolve_includes(body, SNIPPETS_ROOT, _caller(retry_setup=False))
    assert "- name: Hello" not in out
    assert "{{include_if:" not in out
    lines = out.splitlines()
    checkout_idx = next(i for i, line in enumerate(lines) if "actions/checkout" in line)
    after_idx = next(i for i, line in enumerate(lines) if "After" in line)
    assert after_idx - checkout_idx == 1, f"expected checkout → After adjacent, got lines {lines!r}"


def test_conditional_include_defaults_true_when_flag_omitted():
    body = "  {{include_if: retry_setup, snippets/hello.yml}}\n"
    out = _resolve_includes(body, SNIPPETS_ROOT, _caller())
    assert "- name: Hello" in out


def test_conditional_include_unknown_flag_raises():
    body = "  {{include_if: unknown_flag, snippets/hello.yml}}\n"
    with pytest.raises(RenderError, match="unknown conditional flag"):
        _resolve_includes(body, SNIPPETS_ROOT, _caller())


def test_parameterized_snippet_substitutes_args():
    body = 'steps:\n  {{include: snippets/greet_named.yml, STEP_NAME="Alice", STEP_COMMAND="echo bonjour"}}\n'
    out = _resolve_includes(body, SNIPPETS_ROOT, _caller())
    assert "- name: Greet Alice" in out
    assert "run: echo bonjour" in out
    assert "{{STEP_NAME}}" not in out
    assert "{{STEP_COMMAND}}" not in out


def test_parameterized_conditional_snippet():
    body = '  {{include_if: retry_setup, snippets/greet_named.yml, STEP_NAME="Alice", STEP_COMMAND="true"}}\n'
    out = _resolve_includes(body, SNIPPETS_ROOT, _caller(retry_setup=True))
    assert "Greet Alice" in out
    assert "run: true" in out


def test_unsubstituted_placeholder_left_alone_when_no_arg_given():
    body = '  {{include: snippets/greet_named.yml, STEP_COMMAND="pwd"}}\n'
    out = _resolve_includes(body, SNIPPETS_ROOT, _caller())
    assert "{{STEP_NAME}}" in out


def test_bad_param_syntax_raises():
    body = "  {{include: snippets/hello.yml, NO_EQUALS_SIGN}}\n"
    with pytest.raises(RenderError, match="bad include param"):
        _resolve_includes(body, SNIPPETS_ROOT, _caller())


def test_conditional_include_else_branch_expands_when_flag_false(tmp_path):
    snippets = tmp_path / "snippets"
    snippets.mkdir()
    (snippets / "true_branch.yml").write_text("- name: true_branch\n  run: echo T\n")
    (snippets / "false_branch.yml").write_text("- name: false_branch\n  run: echo F\n")
    body = "  {{include_if: retry_setup, snippets/true_branch.yml, else=snippets/false_branch.yml}}\n"
    out_true = _resolve_includes(body, tmp_path, _caller(retry_setup=True))
    out_false = _resolve_includes(body, tmp_path, _caller(retry_setup=False))
    assert "true_branch" in out_true
    assert "false_branch" not in out_true
    assert "false_branch" in out_false
    assert "true_branch" not in out_false
