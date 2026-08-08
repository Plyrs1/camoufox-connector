"""Tests for camoufox_connector.config."""

import pytest
from pydantic import ValidationError

from camoufox_connector.config import Settings, parse_proxy_list, parse_proxy_url


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


class TestProxyParsing:
    """Tests for parse_proxy_url() launch-options mapping."""

    def test_no_credentials(self):
        mapping = parse_proxy_url("http://proxy.example:8080")

        assert mapping == {"server": "http://proxy.example:8080"}

    def test_credentials_without_percent_encoding(self):
        mapping = parse_proxy_url("https://user:pass@proxy.example:8080")

        assert mapping == {
            "server": "https://proxy.example:8080",
            "username": "user",
            "password": "pass",
        }

    def test_credentials_with_percent_encoding(self):
        # %40 -> @, %3A -> : — the server must keep the URL *without* the
        # decoded credentials and the driver must receive the decoded values.
        mapping = parse_proxy_url("http://us%40er:p%40ss@proxy.example:8080")

        assert mapping == {
            "server": "http://proxy.example:8080",
            "username": "us@er",
            "password": "p@ss",
        }

    def test_default_port_is_omitted_from_server(self):
        assert parse_proxy_url("https://proxy.example") == {
            "server": "https://proxy.example"
        }

    def test_socks5_scheme(self):
        mapping = parse_proxy_url("socks5://user:pass@proxy.example:1080")

        assert mapping["server"] == "socks5://proxy.example:1080"
        assert mapping["username"] == "user"

    def test_ipv6_host_is_rebracketed(self):
        assert parse_proxy_url("http://[::1]:8080") == {"server": "http://[::1]:8080"}

    def test_plain_password_plus_encoded_username(self):
        mapping = parse_proxy_url("http://us%40er:pass@proxy.example")

        assert mapping["username"] == "us@er"
        assert mapping["password"] == "pass"

    @pytest.mark.parametrize(
        "url",
        [
            "not-a-url",                     # missing scheme
            "http://",                       # missing hostname
            "http:///path",                  # missing hostname
            "ftp://proxy.example",           # unsupported scheme
            "http://user@proxy.example",     # username without password
            "http://:pass@proxy.example",    # password without username
            "http://proxy.example:notaport",  # invalid port
        ],
    )
    def test_invalid_proxy_urls_are_rejected(self, url):
        with pytest.raises(ValueError):
            parse_proxy_url(url)

    def test_empty_proxy_list_is_empty(self):
        assert parse_proxy_list(None) == []
        assert parse_proxy_list("") == []

    def test_invalid_proxy_rejected_by_settings(self):
        with pytest.raises(ValidationError):
            Settings(geoip=False, proxy="ftp://proxy.example")
        with pytest.raises(ValidationError):
            Settings(geoip=False, proxy="http://user@proxy.example")


class TestProxyAssignment:
    def test_multiple_proxies_parsed_in_order(self):
        settings = Settings(
            geoip=False,
            proxy="http://one.example, https://two.example, socks5://three.example",
        )

        assert settings.proxy_mappings() == [
            {"server": "http://one.example"},
            {"server": "https://two.example"},
            {"server": "socks5://three.example"},
        ]

    def test_single_proxy_applies_to_all_instances(self):
        settings = Settings(geoip=False, proxy="http://one.example")

        assert settings.proxy_mapping(0) == {"server": "http://one.example"}
        assert settings.proxy_mapping(3) == {"server": "http://one.example"}
        assert settings.proxy_mapping(11) == {"server": "http://one.example"}

    def test_proxies_cycle_when_pool_exceeds_proxy_count(self):
        settings = Settings(geoip=False, proxy="http://one.example,http://two.example")

        assert settings.proxy_mapping(0) == {"server": "http://one.example"}
        assert settings.proxy_mapping(1) == {"server": "http://two.example"}
        assert settings.proxy_mapping(2) == {"server": "http://one.example"}  # cycles
        assert settings.proxy_mapping(3) == {"server": "http://two.example"}

    def test_no_proxy_returns_none(self):
        settings = Settings(geoip=False)

        assert settings.proxy_mappings() == []
        assert settings.proxy_mapping(0) is None

    def test_launch_kwargs_map_proxy_by_index(self):
        settings = Settings(
            geoip=False,
            proxy="http://us%40er:p%40ss@one.example:8080,http://two.example",
        )

        kwargs = settings.to_camoufox_kwargs(port=9222, index=0)
        assert kwargs["proxy"] == {
            "server": "http://one.example:8080",
            "username": "us@er",
            "password": "p@ss",
        }
        assert kwargs["port"] == 9222

        kwargs = settings.to_camoufox_kwargs(port=9222, index=1)
        assert kwargs["proxy"] == {"server": "http://two.example"}

        # Cycling: instance 2 returns to the first proxy.
        kwargs = settings.to_camoufox_kwargs(port=9222, index=2)
        assert kwargs["proxy"] == {
            "server": "http://one.example:8080",
            "username": "us@er",
            "password": "p@ss",
        }

    def test_launch_kwargs_without_proxy(self):
        settings = Settings(geoip=False)

        kwargs = settings.to_camoufox_kwargs(port=9222, index=2)
        assert "proxy" not in kwargs
        assert kwargs["port"] == 9222

    def test_launch_kwargs_default_index_is_zero(self):
        settings = Settings(geoip=False, proxy="http://one.example")

        assert settings.to_camoufox_kwargs(port=9222)["proxy"] == {
            "server": "http://one.example"
        }
