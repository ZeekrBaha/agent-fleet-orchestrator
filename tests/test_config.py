"""Tests for fleet.config — written before implementation (TDD RED phase)."""

import pytest

from fleet.config import ConfigurationError, Settings


class TestIsLocalBind:
    """Tests for Settings.is_local_bind()."""

    def test_localhost_ipv4_is_local(self) -> None:
        s = Settings(host="127.0.0.1")
        assert s.is_local_bind() is True

    def test_localhost_ipv6_is_local(self) -> None:
        s = Settings(host="::1")
        assert s.is_local_bind() is True

    def test_all_interfaces_is_not_local(self) -> None:
        s = Settings(host="0.0.0.0")
        assert s.is_local_bind() is False


class TestValidateForStartup:
    """Tests for Settings.validate_for_startup()."""

    def test_local_bind_with_empty_token_succeeds(self) -> None:
        s = Settings(host="127.0.0.1", api_token="")
        # Must not raise
        s.validate_for_startup()

    def test_ipv6_localhost_with_empty_token_succeeds(self) -> None:
        s = Settings(host="::1", api_token="")
        # Must not raise
        s.validate_for_startup()

    def test_non_local_bind_with_empty_token_raises(self) -> None:
        s = Settings(host="0.0.0.0", api_token="")
        with pytest.raises(ConfigurationError):
            s.validate_for_startup()

    def test_non_local_bind_with_token_succeeds(self) -> None:
        s = Settings(host="0.0.0.0", api_token="some-secret-token")
        # Must not raise
        s.validate_for_startup()


class TestSettingsDefaults:
    """Tests for default values."""

    def test_default_host(self) -> None:
        s = Settings()
        assert s.host == "127.0.0.1"

    def test_default_port(self) -> None:
        s = Settings()
        assert s.port == 8000

    def test_default_db_path(self) -> None:
        s = Settings()
        assert s.db_path == "fleet.db"

    def test_default_api_token_is_empty(self) -> None:
        s = Settings()
        assert s.api_token == ""

    def test_default_secret_patterns(self) -> None:
        s = Settings()
        assert s.secret_patterns == ["FLEET_API_TOKEN"]


class TestSettingsEnvOverride:
    """Tests that env vars are picked up correctly."""

    def test_db_path_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLEET_DB_PATH", "/tmp/test-fleet.db")
        s = Settings()
        assert s.db_path == "/tmp/test-fleet.db"
