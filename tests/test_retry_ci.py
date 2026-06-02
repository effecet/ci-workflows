import base64
from unittest.mock import MagicMock

import pytest

from ci_workflows.forgejo import ForgejoError
from ci_workflows.retry_ci import MARKER_PATH, retry_ci


def _marker_b64(content: str) -> dict:
    return {
        "content": base64.b64encode(content.encode()).decode(),
        "sha": "old-sha",
    }


def test_retry_ci_bumps_synced_timestamp_and_puts():
    """Happy path: GET marker → bump synced: → PUT with new content + old sha."""
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.return_value = _marker_b64("version: v2.0.4\ntier: python-app\nsynced: 2026-05-12T08:00:00Z\n")
    client.put_file.return_value = {"commit": {"sha": "new-sha-abc"}}

    new_sha = retry_ci(client, "example-org/example-media")

    assert new_sha == "new-sha-abc"
    # Validate the PUT body
    call = client.put_file.call_args
    assert call.kwargs["path"] == MARKER_PATH
    assert call.kwargs["branch"] == "main"
    assert call.kwargs["sha"] == "old-sha"
    decoded = base64.b64decode(call.kwargs["content_b64"]).decode()
    # synced: line is updated; other lines preserved
    assert "version: v2.0.4" in decoded
    assert "tier: python-app" in decoded
    assert "synced: 2026-05-12T08:00:00Z" not in decoded  # old timestamp gone
    assert "synced: " in decoded  # new timestamp present


def test_retry_ci_uses_explicit_branch_over_default():
    """If branch= is passed, skip get_repo() and use it directly."""
    client = MagicMock()
    client.get_file.return_value = _marker_b64("synced: 2026-01-01T00:00:00Z\n")
    client.put_file.return_value = {"commit": {"sha": "x"}}

    retry_ci(client, "example-org/foo", branch="develop")

    # get_repo NOT called when branch explicit
    client.get_repo.assert_not_called()
    # get_file used the explicit branch
    assert client.get_file.call_args.kwargs["ref"] == "develop"
    # put_file used the explicit branch
    assert client.put_file.call_args.kwargs["branch"] == "develop"


def test_retry_ci_appends_synced_if_missing():
    """If marker has no synced: line (corrupt marker), append one rather than crash."""
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.return_value = _marker_b64("version: v2.0.4\ntier: python-app\n")
    client.put_file.return_value = {"commit": {"sha": "y"}}

    retry_ci(client, "example-org/foo")

    decoded = base64.b64decode(client.put_file.call_args.kwargs["content_b64"]).decode()
    assert "synced: " in decoded
    # Original content preserved
    assert "version: v2.0.4" in decoded


def test_retry_ci_raises_if_marker_missing():
    """If get_file returns non-dict (e.g. empty), raise ForgejoError early — don't PUT."""
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.return_value = []  # directory listing instead of file

    with pytest.raises(ForgejoError, match="no .ci-workflows-version"):
        retry_ci(client, "example-org/foo")
    client.put_file.assert_not_called()


def test_retry_ci_returns_sha_no_op_when_content_unchanged(monkeypatch):
    """If the new timestamp matches the existing (called twice within same second),
    skip the PUT and return the existing sha — no empty commit."""
    fixed_ts = "2026-05-12T12:00:00Z"
    monkeypatch.setattr(
        "ci_workflows.retry_ci.datetime",
        type(
            "FakeDT",
            (),
            {"now": staticmethod(lambda tz: type("FakeNow", (), {"strftime": lambda self, fmt: fixed_ts})())},
        ),
    )
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "main"}
    client.get_file.return_value = _marker_b64(f"synced: {fixed_ts}\n")

    sha = retry_ci(client, "example-org/foo")

    assert sha == "old-sha"  # the existing sha, not a new one
    client.put_file.assert_not_called()


def test_retry_ci_handles_master_default_branch():
    """Callers on `master` (not `main`) work too — default_branch is queried, not assumed."""
    client = MagicMock()
    client.get_repo.return_value = {"default_branch": "master"}
    client.get_file.return_value = _marker_b64("synced: 2026-01-01T00:00:00Z\n")
    client.put_file.return_value = {"commit": {"sha": "z"}}

    retry_ci(client, "example-org/example-app")

    # Both file ops used master
    assert client.get_file.call_args.kwargs["ref"] == "master"
    assert client.put_file.call_args.kwargs["branch"] == "master"
