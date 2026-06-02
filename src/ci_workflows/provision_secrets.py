"""provision_secrets.py — read .env.ci-secrets and PUT each secret to each caller.

The `ALLOWED_SECRETS` allowlist makes CHAT_ID leak architecturally impossible:
even if a future operator drops `TELEGRAM_CI_CHAT_ID` into `.env`, this script
refuses to provision it (chat_id is hardcoded in the notifier snippet —
`templates/_snippets/notify_on_failure.yml`).
"""

import argparse
import os
import sys
from pathlib import Path

from ci_workflows.forgejo import ForgejoClient
from ci_workflows.registry import load_registry

ALLOWED_SECRETS = frozenset({"TELEGRAM_CI_TOKEN"})


class UnknownSecretError(ValueError):
    """Raised when the secrets dict contains a key not in ALLOWED_SECRETS."""


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        if key:
            out[key.strip()] = val
    return out


def provision_all(client, *, callers: list[str], secrets: dict[str, str]) -> None:
    unknown = sorted(set(secrets) - ALLOWED_SECRETS)
    if unknown:
        raise UnknownSecretError(
            f"refusing to provision unknown secret(s): {unknown}. "
            f"Allowlist: {sorted(ALLOWED_SECRETS)}. "
            f"Update ALLOWED_SECRETS in provision_secrets.py if this is intended."
        )
    for repo in callers:
        owner, name = repo.split("/", 1)
        for k, v in secrets.items():
            client.put_secret(owner, name, k, v)
            print(f"  {repo}: set {k}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="callers.yml")
    parser.add_argument(
        "--env",
        default=str(Path.home() / ".env.ci-secrets"),
        help="Path to a file containing the Forgejo API token (FORGEJO_TOKEN=...).",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("CODEBERG_TOKEN")
    if not token:
        print("ERROR: set CODEBERG_TOKEN", file=sys.stderr)
        return 2
    secrets = load_env_file(Path(args.env))
    if not secrets:
        print(f"WARN: {args.env} has no KV pairs; nothing to provision.", file=sys.stderr)
        return 0
    registry = load_registry(Path(args.registry))
    callers = [c.repo for c in registry.callers]
    client = ForgejoClient(base_url="https://codeberg.org", token=token)
    provision_all(client, callers=callers, secrets=secrets)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
