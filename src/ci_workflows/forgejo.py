"""Thin Forgejo REST client — auth, repo, secrets, workflow runs."""

import time
from typing import Any
from urllib.parse import quote

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import Timeout


class ForgejoError(RuntimeError):
    """Any non-2xx response from the Forgejo API."""


class ForgejoClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
    ):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._backoff_base = backoff_base_s
        self._session = requests.Session()

    # --- low-level -----

    def _request(self, method: str, path: str, *, json_body: Any = None) -> Any:
        """Issue a Forgejo API call with retry-with-exponential-backoff on
        network-level errors (Timeout, ConnectionError). HTTP-level errors
        (4xx/5xx) propagate immediately as ForgejoError — they're application
        bugs, not transient infra issues, so retrying would mask them.
        """
        url = f"{self._base}/api/v1{path}"
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    headers={
                        "Authorization": f"token {self._token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json=json_body,
                    timeout=self._timeout,
                )
            except (Timeout, ReqConnectionError) as e:
                last_exc = e
                if attempt + 1 < self._max_retries:
                    time.sleep(self._backoff_base * (2**attempt))  # 1s, 2s, 4s
                    continue
                raise ForgejoError(f"{method} {path} → network error after {self._max_retries} attempts: {e}") from e
            if not resp.ok:
                raise ForgejoError(f"{method} {path} → {resp.status_code}: {resp.text[:500]}")
            if resp.status_code == 204:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        # Loop only exits via return or raise; this is for type checker.
        raise ForgejoError(f"{method} {path} → unreachable: {last_exc}")

    # --- repos -----

    def get_repo(self, owner: str, repo: str) -> dict:
        return self._request("GET", f"/repos/{owner}/{repo}")

    def patch_repo(self, owner: str, repo: str, **fields) -> dict:
        return self._request("PATCH", f"/repos/{owner}/{repo}", json_body=fields)

    # --- secrets -----

    def put_secret(self, owner: str, repo: str, name: str, value: str) -> None:
        self._request(
            "PUT",
            f"/repos/{owner}/{repo}/actions/secrets/{name}",
            json_body={"data": value},
        )

    def list_secrets(self, owner: str, repo: str) -> list[dict]:
        return self._request("GET", f"/repos/{owner}/{repo}/actions/secrets") or []

    def delete_secret(self, owner: str, repo: str, name: str) -> None:
        self._request("DELETE", f"/repos/{owner}/{repo}/actions/secrets/{name}")

    # --- workflow runs -----

    def last_n_runs_on_branch(
        self, owner: str, repo: str, branch: str, *, n: int, from_sha: str | None = None
    ) -> list[dict]:
        """The newest n runs whose ref is `branch`, newest first.

        Forgejo on Codeberg sends no `head_branch` (raw API, verified 2026-11-01
        on example-org/example-app and example-org/ci-workflows). The ref lives in
        `prettyref`: the branch name for a push or schedule run, `#N` for a
        pull_request run, so a PR run can never match a branch. The old "no
        head_branch means this branch" fallback matched every run, so PR and
        sync-branch runs passed a soak meant for the default branch.

        With `from_sha`, only runs from the oldest run of that commit onward
        count, and there are none until that commit has a run. Anchoring on a
        run (Codeberg's clock) rather than the commit's timestamp holds for
        fast-forward and rebase merges, whose commits keep their original time.
        """
        # limit+page: without `page` the API ignores `limit`; 50 is the max page,
        # wide enough that a long soak's PR and sync-branch runs can't push the
        # head's anchor run off the list.
        payload = self._request("GET", f"/repos/{owner}/{repo}/actions/runs?limit=50&page=1")
        runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
        on_branch = sorted(
            (r for r in runs if r.get("prettyref") == branch), key=lambda r: r.get("id", 0), reverse=True
        )
        if from_sha is not None:
            anchors = [i for i, r in enumerate(on_branch) if r.get("commit_sha") == from_sha]
            on_branch = on_branch[: anchors[-1] + 1] if anchors else []
        return on_branch[:n]

    def all_green(self, owner: str, repo: str, branch: str, *, n: int, from_sha: str | None = None) -> bool:
        """Return True only if the last n runs on `branch` (from `from_sha` on) all succeeded.

        Returns False when fewer than n completed runs exist on the branch
        (e.g., a fresh caller that has not accumulated enough history yet).

        Forgejo on Codeberg uses `status` (not GitHub's `conclusion`) for the
        terminal outcome — "success" | "failure" | "running" | "queued".
        """
        runs = self.last_n_runs_on_branch(owner, repo, branch, n=n, from_sha=from_sha)
        if len(runs) < n:
            return False
        return all(r.get("status") == "success" for r in runs)

    def branch_head(self, owner: str, repo: str, branch: str) -> tuple[str, str]:
        """(sha, first line of message) of `branch`'s head commit."""
        payload = self._request("GET", f"/repos/{owner}/{repo}/branches/{quote(branch, safe='')}")
        commit = payload.get("commit") if isinstance(payload, dict) else None
        if not isinstance(commit, dict) or not commit.get("id"):
            raise ForgejoError(f"GET branches/{branch} → no commit id in response")
        lines = (commit.get("message") or "").splitlines()
        return commit["id"], lines[0] if lines else ""

    # --- branches + files (PR plumbing) -----

    def get_file(self, owner: str, repo: str, path: str, *, ref: str = "main") -> dict:
        return self._request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={quote(str(ref), safe='')}")

    def put_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        content_b64: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict:
        body = {"content": content_b64, "message": message, "branch": branch}
        if sha:
            body["sha"] = sha
        return self._request("PUT", f"/repos/{owner}/{repo}/contents/{path}", json_body=body)

    def create_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        content_b64: str,
        message: str,
        branch: str,
    ) -> dict:
        """Create a new file at `path` on `branch`. Forgejo requires POST for creation
        and PUT for update; a PUT without `sha` fails with 422 "[SHA]: Required"
        (verified 2026-04-21 attempting to write .ci-workflows-version on example-app)."""
        body = {"content": content_b64, "message": message, "branch": branch}
        return self._request("POST", f"/repos/{owner}/{repo}/contents/{path}", json_body=body)

    def change_files(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
        message: str,
        files: list[dict],
    ) -> dict:
        """Commit a batch of file operations in a SINGLE commit (repoChangeFiles).

        Codeberg's Forgejo has no Git Data write API (POST /git/trees and
        /git/commits are absent), but POST /contents accepts a `files` array whose
        ops all land in one commit. Used by sync to avoid the multi-commit-per-sync
        storm that left stale push-status failures blocking merge.

        Each entry in `files`:
            {"operation": "create" | "update" | "delete", "path": str,
             "content": <base64 str, for create/update>,
             "sha": <str, required for update/delete>}

        `files` must be non-empty — Forgejo's repoChangeFiles requires it, and
        sync always supplies at least the marker op. Rename (`from_path`/move) is
        intentionally not modeled; sync never renames.
        """
        body = {"branch": branch, "message": message, "files": files}
        return self._request("POST", f"/repos/{owner}/{repo}/contents", json_body=body)

    def list_dir(self, owner: str, repo: str, path: str, *, ref: str = "main") -> list[dict]:
        """List directory contents. Returns array of {name, type, sha, path} items.

        Same endpoint as get_file — Forgejo returns a dict for a file and a list for
        a directory. Use this when the caller expects a list.
        Returns empty list if the directory doesn't exist (404).
        """
        try:
            got = self._request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={quote(str(ref), safe='')}")
        except ForgejoError:
            return []
        return got if isinstance(got, list) else []

    def delete_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        sha: str,
        message: str,
        branch: str,
    ) -> dict:
        """Delete a file. Forgejo DELETE /contents/{path} requires the current sha."""
        body = {"sha": sha, "message": message, "branch": branch}
        return self._request("DELETE", f"/repos/{owner}/{repo}/contents/{path}", json_body=body)

    def create_branch(self, owner: str, repo: str, *, new_branch: str, from_ref: str) -> dict:
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/branches",
            json_body={"new_branch_name": new_branch, "old_branch_name": from_ref},
        )

    def list_pulls(self, owner: str, repo: str, *, state: str = "open") -> list[dict]:
        return self._request("GET", f"/repos/{owner}/{repo}/pulls?state={quote(str(state), safe='')}") or []

    def create_pull(self, owner: str, repo: str, *, title: str, head: str, base: str, body: str) -> dict:
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json_body={"title": title, "head": head, "base": base, "body": body},
        )
