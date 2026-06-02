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
    assert len(runs) == 3
    assert all(r["head_branch"] == "main" for r in runs)
    assert all(r["status"] == "success" for r in runs)


def test_last_n_runs_on_branch_tolerates_empty_head_branch():
    """Forgejo on Codeberg returns head_branch=null for push events (verified via raw
    API 2026-04-21 on /repos/example-org/ci-workflows/actions/runs). The MCP coerces null to
    "" — my client must tolerate BOTH None and "" as "matches the asked-for branch"
    since push events only fire on a single branch anyway.
    """
    client = ForgejoClient(base_url="https://codeberg.org", token="tok")
    payload = {
        "workflow_runs": [
            {"id": 1, "head_branch": None, "status": "success"},  # raw API shape
            {"id": 2, "head_branch": "", "status": "success"},  # MCP-coerced shape
            {"id": 3, "head_branch": "dev", "status": "success"},
        ]
    }
    with patch.object(client._session, "request", return_value=_mock_response(200, payload)):
        runs = client.last_n_runs_on_branch("example-org", "ci-workflows", "main", n=5)
    assert [r["id"] for r in runs] == [1, 2]


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
