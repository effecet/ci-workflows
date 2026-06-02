from unittest.mock import MagicMock

import pytest

from ci_workflows.provision_secrets import (
    ALLOWED_SECRETS,
    UnknownSecretError,
    load_env_file,
    provision_all,
)


def test_load_env_file_parses_kv(tmp_path):
    f = tmp_path / ".env.ci-secrets"
    f.write_text("TELEGRAM_CI_TOKEN=123:abc\nTELEGRAM_CI_CHAT_ID=-100\n# comment\n\n")
    got = load_env_file(f)
    assert got == {"TELEGRAM_CI_TOKEN": "123:abc", "TELEGRAM_CI_CHAT_ID": "-100"}


def test_load_env_file_ignores_quotes_and_whitespace(tmp_path):
    f = tmp_path / ".env"
    f.write_text('FOO="bar baz"\nBAZ=quux  \n')
    got = load_env_file(f)
    assert got == {"FOO": "bar baz", "BAZ": "quux"}


def test_provision_all_puts_allowed_secret_to_each_caller():
    client = MagicMock()
    secrets = {"TELEGRAM_CI_TOKEN": "123:abc"}
    callers = ["example-org/alpha", "example-org/beta"]
    provision_all(client, callers=callers, secrets=secrets)
    # 2 callers × 1 allowed secret = 2 PUTs
    assert client.put_secret.call_count == 2


def test_allowlist_contains_only_telegram_token():
    assert ALLOWED_SECRETS == frozenset({"TELEGRAM_CI_TOKEN"})


def test_provision_all_filters_chat_id_out():
    """CHAT_ID must NOT be provisioned as a secret — it's hardcoded in the
    notifier template (`reference_telegram_parse_mode_html` + Phase 1 spec
    decision 8). Loading both keys from .env should raise rather than
    silently ship CHAT_ID to every caller."""
    client = MagicMock()
    secrets = {"TELEGRAM_CI_TOKEN": "abc", "TELEGRAM_CI_CHAT_ID": "123"}
    with pytest.raises(UnknownSecretError, match="TELEGRAM_CI_CHAT_ID"):
        provision_all(client, callers=["example-org/alpha"], secrets=secrets)
    # Nothing was provisioned — fail-fast before any PUT.
    assert client.put_secret.call_count == 0


def test_provision_all_raises_on_unknown_secret():
    client = MagicMock()
    secrets = {"SOMETHING_UNRELATED": "x"}
    with pytest.raises(UnknownSecretError, match="SOMETHING_UNRELATED"):
        provision_all(client, callers=["example-org/alpha"], secrets=secrets)
    assert client.put_secret.call_count == 0
