import json

import pytest

from fucc import network


@pytest.mark.parametrize("value,expected", [("AS16509", "AS16509"), ("as16509 Amazon.com", "AS16509"), (" AS8075 Microsoft", "AS8075"), ("AS165090", "AS165090"), ("AS16509evil", None), ("notAS16509", None), ("Organization AS16509", None), (None, None), (16509, None), ("AS", None)])
def test_asn_tokens_are_exact(value, expected):
    assert network.normalize_asn(value) == expected


@pytest.mark.parametrize("value", ["https://user:secret@example.com/path?token=private", "user:secret@example.com:8080/path", "socks5://user:secret@example.com", "https://user:secret@[2001:db8::1]:8080/path"])
def test_url_redaction_removes_userinfo_path_query_and_ports(value):
    redacted = network.redact_url(value)
    assert "secret" not in redacted and "user" not in redacted and "private" not in redacted
    assert "path" not in redacted and "8080" not in redacted
    assert redacted.startswith("***@")


def test_url_redaction_handles_malformed_input():
    assert network.redact_url("https://user:secret@[broken") == "(unparseable; hidden)"
    assert network.redact_url("https://example.com/a?q=secret") == "example.com"


def test_allowlist_only_reads_explicit_user_locations(tmp_path, monkeypatch):
    environment = {"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    (tmp_path / "corporate-allowlist.json").write_text('{"allowAsns":["AS16509"]}')
    monkeypatch.chdir(tmp_path)
    assert not network.corporate(asn="AS16509", environment=environment)
    path = tmp_path / "config" / "fucc" / "corporate-allowlist.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"allowCidrs":[" 203.0.113.0/24 ","2001:db8::/32","invalid"],"allowAsns":["AS8075 Microsoft","AS16509evil",null]}')
    assert network.corporate(ip="203.0.113.2", environment=environment)
    assert network.corporate(ip="2001:db8::2", environment=environment)
    assert network.corporate(asn="as8075", environment=environment)
    assert not network.corporate(asn="AS16509evil", environment=environment)
    assert not network.corporate(asn="ASnotvalid", environment=environment)
    override = tmp_path / "override.json"
    override.write_text('{"allowAsns":["AS16509"],"allowCidrs":[]}')
    assert network.corporate(asn="AS16509", environment={**environment, "FUCC_CORPORATE_ALLOWLIST": str(override)})


def payload(ip="203.0.113.1", country="US", asn="AS16509", timezone="America/New_York"):
    return {"ip": ip, "country": country, "region": "Region", "city": "City", "asn": asn, "org": "Example", "timezone": timezone}


@pytest.mark.parametrize("secondary,consistent", [
    (payload(), True),
    (payload(ip="203.0.113.2"), False),
    (payload(ip="2001:db8::1"), True),
    (payload(ip="2001:db8::1", country="JP"), False),
    (payload(ip="2001:db8::1", asn="AS8075"), False),
])
def test_multiple_source_ipv4_ipv6_consistency(tmp_path, secondary, consistent):
    result = network.merge_intelligence(payload(), secondary, {"HOME": str(tmp_path)})
    assert result["sourceCount"] == 2
    assert result["multiSourceConsistent"] is consistent
    assert result["ipType"] == "datacenter"


def test_ipv6_spellings_compare_as_the_same_address(tmp_path):
    result = network.merge_intelligence(payload(ip="2001:db8::1"), payload(ip="2001:0db8:0:0:0:0:0:1"), {"HOME": str(tmp_path)})
    assert result["multiSourceConsistent"]


def test_datacenter_exact_match_and_unknown_residential_status(tmp_path):
    environment = {"HOME": str(tmp_path)}
    assert network.merge_intelligence(payload(asn="AS165090"), environment=environment)["ipType"] == "unknown"
    assert network.merge_intelligence(payload(asn="AS12345"), environment=environment)["ipType"] == "unknown"
    assert network.merge_intelligence(payload(asn=None), environment=environment)["ipType"] == "unknown"


def test_provider_payloads_normalize_asn_and_ip():
    result = network.source_payload("ipwho", {"ip": "2001:0db8::1", "country_code": "US", "connection": {"asn": 16509, "org": "Amazon"}, "timezone": {"id": "America/New_York"}})
    assert result["ip"] == "2001:db8::1" and result["asn"] == "AS16509"
    assert network.source_payload("ipinfo", {"ip": "203.0.113.1", "org": "AS16509 Amazon"})["asn"] == "AS16509"
    for value in ({"ip": 1234}, {"ip": True}, {"ip": "invalid"}, {"ip": "203.0.113.1", "success": False}, [], None):
        assert network.source_payload("ipwho", value) is None


def test_sources_fail_independently_without_leaking_exception_credentials(tmp_path):
    def fetcher(url):
        if "ipwho" in url:
            raise RuntimeError("user:secret@proxy")
        return {"ip": "203.0.113.1", "country": "US", "org": "AS12345 Example"}
    result = network.fetch_ip_intelligence({"HOME": str(tmp_path)}, fetcher)
    assert result["sourceCount"] == 1
    assert "secret" not in json.dumps(result)
    assert network.fetch_ip_intelligence({}, lambda url: {"success": False}) is None


def test_fetch_has_proxy_precedence_timeout_and_response_bound(monkeypatch):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            observed["limit"] = limit
            return b'{"ip":"203.0.113.1"}'

    class Opener:
        def open(self, request, timeout):
            observed["timeout"] = timeout
            observed["agent"] = request.headers["User-agent"]
            return Response()

    def build(handler):
        observed["proxies"] = handler.proxies
        return Opener()

    monkeypatch.setattr(network.urllib.request, "build_opener", build)
    assert network.fetch_json("https://example.test", {"https_proxy": "http://first", "HTTPS_PROXY": "http://second", "HTTP_PROXY": "http://third"}) == {"ip": "203.0.113.1"}
    assert observed == {"proxies": {"http": "http://first", "https": "http://first"}, "timeout": 5, "agent": "FUCC/0.0.2", "limit": 1024 * 1024 + 1}
    monkeypatch.setattr(Response, "read", lambda self, limit: b"x" * limit)
    with pytest.raises(ValueError, match="size limit"):
        network.fetch_json("https://example.test", {})


def test_secondary_timezone_fills_missing_primary_but_preserves_both_observations(tmp_path):
    result = network.merge_intelligence(payload(timezone=None), payload(timezone="America/Los_Angeles"), {"HOME": str(tmp_path)},
                                        source_names=["ipwho.is", "ipinfo.io"])
    assert result["timezone"] == "America/Los_Angeles"
    assert result["timezoneSources"] == [
        {"source": "ipwho.is", "country": "US", "timezone": None},
        {"source": "ipinfo.io", "country": "US", "timezone": "America/Los_Angeles"},
    ]


def test_conflicting_provider_zones_remain_visible(tmp_path):
    result = network.merge_intelligence(payload(timezone="America/Phoenix"), payload(timezone="America/Denver"), {"HOME": str(tmp_path)})
    assert result["multiSourceConsistent"] is True
    assert [item["timezone"] for item in result["timezoneSources"]] == ["America/Phoenix", "America/Denver"]
    from fucc.regions import resolve_timezone
    assert resolve_timezone("us", result, "America/Chicago")["source"] == "saved"


def test_fetch_tracks_actual_provider_when_primary_fails(tmp_path):
    def fetcher(url):
        if "ipwho" in url:
            raise OSError("offline")
        return {"ip": "203.0.113.1", "country": "US", "timezone": "America/Phoenix"}
    result = network.fetch_ip_intelligence({"HOME": str(tmp_path)}, fetcher)
    assert result["timezoneSources"] == [{"source": "ipinfo.io", "country": "US", "timezone": "America/Phoenix"}]
    assert result["sourceCount"] == 1


def test_provider_formats_reach_timezone_selector_without_extra_lookup(tmp_path):
    def fetcher(url):
        if "ipwho" in url:
            return {"ip": "203.0.113.1", "country_code": "US", "timezone": {"id": "America/Los_Angeles"}}
        return {"ip": "203.0.113.1", "country": "US", "timezone": "America/Los_Angeles"}
    result = network.fetch_ip_intelligence({"HOME": str(tmp_path)}, fetcher)
    from fucc.regions import resolve_timezone
    assert resolve_timezone("us", result)["timezone"] == "America/Los_Angeles"


def test_corporate_asn_exemption_preserves_geo_disagreement(tmp_path):
    path = tmp_path / ".config/fucc/corporate-allowlist.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"allowAsns":["AS16509"],"allowCidrs":[]}')
    result = network.merge_intelligence(payload(), payload(ip="203.0.113.2"), {"HOME": str(tmp_path)})
    assert result["multiSourceConsistent"] is True
    assert result["geolocationConsistent"] is False
    from fucc.regions import resolve_timezone
    assert resolve_timezone("us", result)["source"] == "default"
