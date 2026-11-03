import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ci_workflows.forgejo import ForgejoClient, ForgejoError

RESP = Path(__file__).parent / "fixtures" / "forgejo_responses"


def _mock_response(status: int, json_payload=None, text=""):
    m = MagicMock()
    m.status_code = status
    m.ok = 200 <= status < 300
    m.json.return_value = json_payload or {}
    m.text = text if text else json.dumps(json_payload or {})
    return m


def test_get_repo_returns_payload_on_200():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    payload = json.loads((RESP / "repo_get.json").read_text())
    with patch.object(client._session, "request", return_value=_mock_response(200, payload)) as req:
        got = client.get_repo("example-org", "example-app")
    assert got["full_name"] == "example-org/example-app"
    assert got["has_actions"] is True
    req.assert_called_once()
    args, kwargs = req.call_args
    assert kwargs["headers"]["Authorization"] == "token tok"


def test_put_secret_sends_data_payload():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    with patch.object(client._session, "request", return_value=_mock_response(201)) as req:
        client.put_secret("example-org", "example-app", "FOO", "bar")
    _, kwargs = req.call_args
    assert kwargs["method"] == "PUT"
    assert kwargs["url"].endswith("/repos/example-org/example-app/actions/secrets/FOO")
    assert kwargs["json"] == {"data": "bar"}


def test_delete_secret_accepts_204():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    with patch.object(client._session, "request", return_value=_mock_response(204)):
        client.delete_secret("example-org", "example-app", "FOO")  # no exception = success


def test_api_error_raises_forgejoerror():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    with patch.object(client._session, "request", return_value=_mock_response(403, text="Forbidden")):
        with pytest.raises(ForgejoError, match="403"):
            client.get_repo("example-org", "example-app")


def test_retries_on_timeout_then_succeeds(monkeypatch):
    """Timeout on attempts 1+2, success on attempt 3 → final return is the success payload."""
    from requests.exceptions import Timeout

    client = ForgejoClient(base_url="https://codeberg.org", token="tok", max_retries=3, backoff_base_s=0)
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    call_count = {"n": 0}

    def fake_request(**kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise Timeout("read timeout")
        return _mock_response(200, {"ok": True})

    with patch.object(client._session, "request", side_effect=fake_request):
        got = client.get_repo("example-org", "example-app")
    assert got == {"ok": True}
    assert call_count["n"] == 3
    # Sleep called 2 times (between attempts 1→2 and 2→3); not after final success.
    assert len(sleep_calls) == 2


def test_retries_on_connection_error(monkeypatch):
    """ConnectionError is also retried (not just Timeout)."""
    from requests.exceptions import ConnectionError as ReqConnectionError

    client = ForgejoClient(base_url="https://codeberg.org", token="tok", max_retries=2, backoff_base_s=0)
    monkeypatch.setattr("time.sleep", lambda _: None)
    call_count = {"n": 0}

    def fake_request(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ReqConnectionError("conn refused")
        return _mock_response(200, {"ok": True})

    with patch.object(client._session, "request", side_effect=fake_request):
        client.get_repo("example-org", "example-app")
    assert call_count["n"] == 2


def test_exhausts_retries_raises_forgejoerror(monkeypatch):
    """All max_retries attempts fail with Timeout → final ForgejoError with attempt count."""
    from requests.exceptions import Timeout

    client = ForgejoClient(base_url="https://codeberg.org", token="tok", max_retries=3, backoff_base_s=0)
    monkeypatch.setattr("time.sleep", lambda _: None)

    with patch.object(client._session, "request", side_effect=Timeout("always slow")):
        with pytest.raises(ForgejoError, match="network error after 3 attempts"):
            client.get_repo("example-org", "example-app")


def test_no_retry_on_4xx():
    """HTTP errors (4xx) are application-level and should NOT trigger retry.
    Retrying a 403 would mask a real auth problem.
    """
    client = ForgejoClient(base_url="https://codeberg.org", token="tok", max_retries=3)
    with patch.object(client._session, "request", return_value=_mock_response(403, text="Forbidden")) as req:
        with pytest.raises(ForgejoError, match="403"):
            client.get_repo("example-org", "example-app")
    # Exactly ONE call, no retry on 4xx.
    assert req.call_count == 1


def test_no_retry_on_5xx():
    """HTTP 500 series — same rationale as 4xx — application-level errors don't retry.
    (Future: could selectively retry 502/503/504 transient gateway errors; not today.)
    """
    client = ForgejoClient(base_url="https://codeberg.org", token="tok", max_retries=3)
    with patch.object(
        client._session, "request", return_value=_mock_response(500, text="Internal Server Error")
    ) as req:
        with pytest.raises(ForgejoError, match="500"):
            client.get_repo("example-org", "example-app")
    assert req.call_count == 1


def test_default_timeout_is_60s():
    """Default timeout bumped 30s → 60s (2026-05-12: a notebook-heavy caller needed >30s)."""
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    assert client._timeout == 60.0


def test_backoff_uses_exponential_delay(monkeypatch):
    """Backoff sequence: base, base*2, base*4. Verify with base=2s → [2, 4]."""
    from requests.exceptions import Timeout

    client = ForgejoClient(base_url="https://codeberg.org", token="tok", max_retries=3, backoff_base_s=2.0)
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    with patch.object(client._session, "request", side_effect=Timeout()):
        with pytest.raises(ForgejoError):
            client.get_repo("example-org", "example-app")
    # 3 attempts, 2 sleeps between them (after attempt 1 and 2; not after attempt 3 which raises).
    assert sleep_calls == [2.0, 4.0]


def test_last_n_runs_on_branch_filters_by_branch_and_status():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    payload = json.loads((RESP / "tasks_list_green.json").read_text())
    with patch.object(client._session, "request", return_value=_mock_response(200, payload)):
        runs = client.last_n_runs_on_branch("example-org", "ci-workflows", "main", n=3)
    assert [r["id"] for r in runs] == [103, 102, 101]


def test_last_n_runs_on_branch_excludes_pr_and_other_branch_pushes():
    """Codeberg sends no head_branch at all; the branch is `prettyref` (`#N` for a PR).

    Raw GET /repos/example-org/example-app/actions/runs, 2026-11-01: run #199 push
    prettyref=master, #198 pull_request prettyref=#21, #197 push
    prettyref=sync/v3.3.1. The old `not head_branch` fallback counted all three as
    master, so the v3.3.1 soak passed on one real master run.
    """
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    payload = {
        "workflow_runs": [
            {"id": 199, "event": "push", "prettyref": "master", "status": "success"},
            {"id": 198, "event": "pull_request", "prettyref": "#21", "status": "success"},
            {"id": 197, "event": "push", "prettyref": "sync/v3.3.1", "status": "success"},
            {"id": 196, "event": "push", "head_branch": None, "status": "success"},
            {"id": 194, "event": "push", "head_branch": "master", "status": "success"},
            {"id": 195, "event": "push", "prettyref": "master", "status": "success"},
        ]
    }
    with patch.object(client._session, "request", return_value=_mock_response(200, payload)):
        runs = client.last_n_runs_on_branch("example-org", "example-app", "master", n=5)
    # No ref at all, or only the never-sent head_branch, is not "on master";
    # and the result is newest first even when the API lists out of order.
    assert [r["id"] for r in runs] == [199, 195]


# example-app master after the v3.3.1 pilot merge (raw API, 2026-11-01).
_PILOT_RUNS = {
    "workflow_runs": [
        {"id": 200, "event": "push", "prettyref": "master", "commit_sha": "a1b2c3d4", "status": "success"},
        {"id": 199, "event": "push", "prettyref": "master", "commit_sha": "a1b2c3d4", "status": "success"},
        {"id": 198, "event": "pull_request", "prettyref": "#21", "commit_sha": "b2c3d4e5", "status": "success"},
        {"id": 195, "event": "push", "prettyref": "master", "commit_sha": "c3d4e5f6", "status": "success"},
    ]
}


def test_from_sha_counts_only_runs_from_the_heads_first_run_on():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    with patch.object(client._session, "request", return_value=_mock_response(200, _PILOT_RUNS)):
        runs = client.last_n_runs_on_branch("example-org", "example-app", "master", n=5, from_sha="a1b2c3d4")
        assert [r["id"] for r in runs] == [200, 199]
        # Run #195 is pre-merge (old templates): 3 green must not pass yet.
        assert client.all_green("example-org", "example-app", "master", n=3, from_sha="a1b2c3d4") is False
        assert client.all_green("example-org", "example-app", "master", n=2, from_sha="a1b2c3d4") is True


def test_from_sha_with_no_run_yet_counts_nothing():
    """A head with no run yet (CI not started) must not fall back to older runs."""
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    with patch.object(client._session, "request", return_value=_mock_response(200, _PILOT_RUNS)):
        assert client.last_n_runs_on_branch("example-org", "example-app", "master", n=5, from_sha="ffffffff") == []
        assert client.all_green("example-org", "example-app", "master", n=1, from_sha="ffffffff") is False


def test_last_n_runs_on_branch_requests_a_full_page():
    """Without `page` the API ignores `limit`; a short default page lets a long
    soak's PR runs push the head's anchor run off the list."""
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    with patch.object(client._session, "request", return_value=_mock_response(200, {"workflow_runs": []})) as req:
        client.last_n_runs_on_branch("example-org", "example-app", "master", n=3)
    assert req.call_args.kwargs["url"].endswith("/repos/example-org/example-app/actions/runs?limit=50&page=1")


def test_branch_head_with_empty_message_returns_empty_subject():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    payload = {"name": "master", "commit": {"id": "a1b2c3d4", "message": ""}}
    with patch.object(client._session, "request", return_value=_mock_response(200, payload)):
        assert client.branch_head("example-org", "example-app", "master") == ("a1b2c3d4", "")


def test_branch_head_returns_sha_and_subject():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    payload = {"name": "master", "commit": {"id": "a1b2c3d4", "message": "Merge pull request #21\n\nbody"}}
    with patch.object(client._session, "request", return_value=_mock_response(200, payload)) as req:
        assert client.branch_head("example-org", "example-app", "master") == ("a1b2c3d4", "Merge pull request #21")
    assert req.call_args.kwargs["url"].endswith("/repos/example-org/example-app/branches/master")


def test_branch_head_without_commit_raises_forgejo_error():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    with patch.object(client._session, "request", return_value=_mock_response(200, {"name": "master"})):
        with pytest.raises(ForgejoError, match="no commit id"):
            client.branch_head("example-org", "example-app", "master")


def test_all_green_returns_true_for_all_success():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    payload = json.loads((RESP / "tasks_list_green.json").read_text())
    with patch.object(client._session, "request", return_value=_mock_response(200, payload)):
        assert client.all_green("example-org", "ci-workflows", "main", n=3) is True


def test_all_green_returns_false_if_any_failed():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    payload = json.loads((RESP / "tasks_list_red.json").read_text())
    with patch.object(client._session, "request", return_value=_mock_response(200, payload)):
        assert client.all_green("example-org", "ci-workflows", "main", n=2) is False


def test_change_files_posts_batch_to_contents():
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    files = [
        {"operation": "create", "path": ".ci-workflows-version", "content": "eA=="},
        {"operation": "update", "path": ".github/workflows/ci.yml", "content": "eA==", "sha": "abc"},
        {"operation": "delete", "path": ".forgejo/workflows/legacy.yml", "sha": "def"},
    ]
    with patch.object(client._session, "request", return_value=_mock_response(201, {"commit": {"sha": "z"}})) as req:
        got = client.change_files("example-org", "alpha", branch="sync/v1", message="sync: bump to v1", files=files)
    assert got["commit"]["sha"] == "z"
    _, kwargs = req.call_args
    assert kwargs["method"] == "POST"
    assert kwargs["url"].endswith("/repos/example-org/alpha/contents")
    assert kwargs["json"] == {"branch": "sync/v1", "message": "sync: bump to v1", "files": files}
