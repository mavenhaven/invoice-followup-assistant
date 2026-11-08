"""Test hygiene shared by every test file."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_real_secrets(tmp_path):
    """Tests must never see the developer's real .streamlit/secrets.toml.

    Streamlit's page-test harness reads the real secrets file unless a test supplies its own. With real credentials
    in place, a test that forgot to pass a fake sheet would connect to the REAL Google Sheet and write to it. Pointing
    Streamlit at a file that does not exist makes every test start with no secrets at all.
    """
    from streamlit import config
    from streamlit.runtime.secrets import secrets_singleton

    previous = config.get_option("secrets.files")
    config.set_option("secrets.files", [str(tmp_path / "no-secrets-here.toml")])
    secrets_singleton._secrets = None  # forget anything already loaded, so the next access re-reads the (empty) list
    yield
    config.set_option("secrets.files", previous)
    secrets_singleton._secrets = None
