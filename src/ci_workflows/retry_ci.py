"""retry_ci.py — bump a caller's .ci-workflows-version marker to retrigger CI.

Forgejo's REST API doesn't expose a workflow-run cancel/rerun endpoint
(only the Web UI does), so the practical "retry CI" pattern is to push
a new commit that supersedes whatever is queued or stuck. Updating only
the `synced:` timestamp on .ci-workflows-version produces a minimal,
auditable commit (visible in git log) without touching templated content.

Used 2 ways:
  - As a library:  from ci_workflows.retry_ci import retry_ci; retry_ci(client, "example-org/foo")
  - As a CLI:      ci-retry --caller=example-org/foo [--branch=main]

Side-effect-only: returns the new commit sha on success, raises ForgejoError
otherwise. Branch defaults to the repo's `default_branch` if not given.
"""

import argparse
import base64
import os
import sys
from datetime import UTC, datetime

from ci_workflows.forgejo import ForgejoClient, ForgejoError

MARKER_PATH = ".ci-workflows-version"


def retry_ci(client: ForgejoClient, caller_repo: str, *, branch: str | None = None) -> str:
    """Bump the synced: timestamp on the marker file to trigger a fresh CI run.

    Args:
        client: authenticated ForgejoClient
        caller_repo: "owner/name" form (e.g. "example-org/example-media")
        branch: branch to push to; defaults to the repo's default_branch.

    Returns: the new commit sha (string).
    Raises: ForgejoError if marker is missing, branch unknown, or PUT fails.
    """
    owner, repo = caller_repo.split("/", 1)
    if branch is None:
        branch = client.get_repo(owner, repo).get("default_branch", "main")

    # Read current marker
    obj = client.get_file(owner, repo, MARKER_PATH, ref=branch)
    if not isinstance(obj, dict) or "content" not in obj:
        raise ForgejoError(f"{caller_repo}: no .ci-workflows-version on {branch}; nothing to bump")
    content = base64.b64decode(obj["content"]).decode()
    sha = obj["sha"]

    # Bump synced timestamp (preserve everything else)
    new_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_lines = []
    found_synced = False
    for line in content.splitlines():
        if line.startswith("synced:"):
            new_lines.append(f"synced: {new_ts}")
            found_synced = True
        else:
            new_lines.append(line)
    if not found_synced:
        new_lines.append(f"synced: {new_ts}")
    new_content = "\n".join(new_lines) + "\n"

    if new_content == content:
        # Same timestamp (called twice within 1s) — no-op rather than empty commit.
        # The existing run is still recent enough.
        return sha

    new_b64 = base64.b64encode(new_content.encode()).decode()
    result = client.put_file(
        owner,
        repo,
        path=MARKER_PATH,
        content_b64=new_b64,
        message="[ci] chore: refresh marker to retrigger CI",
        branch=branch,
        sha=sha,
    )
    return result.get("commit", {}).get("sha", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrigger CI by bumping the marker file.")
    parser.add_argument("--caller", required=True, help="owner/name (e.g. example-org/example-media)")
    parser.add_argument("--branch", default=None, help="branch (default: repo's default_branch)")
    args = parser.parse_args(argv)

    token = os.environ.get("CODEBERG_TOKEN")
    if not token:
        print("ERROR: set CODEBERG_TOKEN (example-org-scope).", file=sys.stderr)
        return 2

    client = ForgejoClient(base_url="https://codeberg.org", token=token)
    try:
        new_sha = retry_ci(client, args.caller, branch=args.branch)
    except ForgejoError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"{args.caller}: bumped marker → commit {new_sha[:12] if new_sha else '(no-op)'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
