"""Tests for camoufox_connector.config."""

import pytest
from pydantic import ValidationError

from camoufox_connector.config import Settings


def test_public_ws_url_is_normalized():
    settings = Settings(geoip=False, public_ws_url="wss://browser.example.com/")

    assert settings.public_ws_url == "wss://browser.example.com"
    assert settings.get_public_ws_base_url() == "wss://browser.example.com"


def test_public_ws_url_defaults_to_localhost_api_port():
    settings = Settings(geoip=False, api_port=9090)

    assert settings.get_public_ws_base_url() == "ws://localhost:9090"


def test_public_ws_url_requires_websocket_scheme():
    with pytest.raises(ValidationError):
        Settings(geoip=False, public_ws_url="https://browser.example.com")
