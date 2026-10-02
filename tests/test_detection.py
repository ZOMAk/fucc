import json
import locale
from datetime import datetime, timezone

import pytest

from fucc import detection


def by_id(result, identity):
    return next(item for item in result["signals"] if item["id"] == identity)


def detect(environment, **kwargs):
    return detection.detect("us", environment, resolver=lambda host: "1.1.1.1", fonts=lambda: "Noto CJK\nNoto CJK\n", python_locale="en_US.UTF-8", **kwargs)


def test_limited_signals_have_no_contribution_or_denominator_weight():
    signals = [detection.signal("known", "Known", "bad", 10, 1), detection.signal("unknown", "Unknown", "none", 90, 1, availability="limited")]
    assert signals[1]["contribution"] == 0
    assert detection.calculate_score(signals) == 100
    assert detection.calculate_score(signals[1:]) == 0


def test_unknown_timezone_and_missing_ip_are_detection_limits(tmp_path):
    result = detect({"HOME": str(tmp_path), "TZ": "Invalid/Zone", "LANG": "en_US.UTF-8"})
    assert result["score"] == 0
    assert by_id(result, "timezone")["availability"] == "limited"
    assert by_id(result, "utc-offset")["availability"] == "limited"
    assert by_id(result, "ip-intelligence")["availability"] == "limited"
    assert by_id(result, "consistency")["contribution"] == 0
    assert result["status"] == "unknown" and result["accessVerified"] is False
    assert result["diagnosticRuntime"] == "python"
    assert by_id(result, "locale")["label"] == "Python Locale"


def test_unknown_python_locale_is_not_a_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(detection.locale, "setlocale", lambda category: (_ for _ in ()).throw(locale.Error("not available")))
    result = detection.detect("us", {"HOME": str(tmp_path), "TZ": "America/New_York", "LANG": "en_US.UTF-8"}, resolver=lambda host: "1.1.1.1", fonts=lambda: "")
    assert by_id(result, "locale")["availability"] == "limited"
    assert by_id(result, "consistency")["contribution"] == 0
    assert result["score"] == 0


@pytest.mark.parametrize("instant,offset", [(datetime(2026, 1, 1, tzinfo=timezone.utc), "UTC-05:00"), (datetime(2026, 7, 1, tzinfo=timezone.utc), "UTC-04:00")])
def test_zoneinfo_uses_the_same_instant_including_dst(tmp_path, instant, offset):
    result = detect({"HOME": str(tmp_path), "TZ": "America/New_York", "LANG": "en_US.UTF-8"}, now=instant)
    assert by_id(result, "utc-offset")["value"] == offset
    assert by_id(result, "utc-offset")["score"] == 0


def test_london_dst_changes_expected_offset_against_utc(tmp_path):
    environment = {"HOME": str(tmp_path), "TZ": "UTC", "LANG": "en_GB.UTF-8"}
    for month, score in ((1, 0), (7, 1)):
        result = detection.detect("eu", environment, resolver=lambda host: "1.1.1.1", fonts=lambda: "", python_locale="en_GB.UTF-8", now=datetime(2026, month, 1, tzinfo=timezone.utc))
        assert by_id(result, "utc-offset")["score"] == score


def test_locale_matching_does_not_accept_substring_impostors(tmp_path):
    result = detect({"HOME": str(tmp_path), "TZ": "America/New_York", "LC_ALL": "evil_en_US.UTF-8", "LANG": "en_US.UTF-8"})
    assert by_id(result, "language")["score"] == 0.5
    result = detection.detect("us", {"HOME": str(tmp_path), "TZ": "America/New_York", "LANG": "en-US.UTF-8"}, resolver=lambda host: "1.1.1.1", fonts=lambda: "", python_locale="en-US")
    assert by_id(result, "language")["score"] == by_id(result, "locale")["score"] == 0


def test_base_url_and_proxy_credentials_do_not_enter_report(tmp_path):
    environment = {"HOME": str(tmp_path), "TZ": "America/New_York", "LANG": "en_US.UTF-8", "ANTHROPIC_BASE_URL": "https://user:secret@api.deepseek.com/v1?api_key=private", "https_proxy": "http://proxy:password@example.com:8888"}
    result = detect(environment)
    serialized = json.dumps(result)
    for token in ("secret", "private", "password", "proxy:", "8888"):
        assert token not in serialized
    assert by_id(result, "base-url")["score"] == 1
    assert by_id(result, "base-url")["value"] == "***@api.deepseek.com"
    assert by_id(result, "proxy-env")["value"] == "Configured: https_proxy"
    result = detect({**environment, "ANTHROPIC_BASE_URL": "https://api.deepseek.com.attacker.example/v1"})
    assert by_id(result, "base-url")["score"] == 0


@pytest.mark.parametrize("address,score", [("198.18.0.1", 0), ("127.0.0.1", 0.75), ("10.0.0.1", 0.75), ("100.64.0.1", 0.75), ("1.1.1.1", 0)])
def test_dns_observations_do_not_label_fake_ip_as_pollution(address, score):
    result = detection.dns_signal(lambda host: address)
    assert result["score"] == score
    assert "pollution" not in result["value"]
    if address.startswith("198.18"):
        assert "Clash fake-ip" in result["value"]


def test_dns_and_fonts_unavailable_do_not_score():
    def fail(*args):
        raise OSError("unavailable")
    for result in (detection.dns_signal(fail), detection.font_signal(fail)):
        assert result["availability"] == "limited"
        assert result["contribution"] == 0


@pytest.mark.parametrize("score,level", [(0, "low"), (20, "low"), (21, "medium"), (50, "medium"), (51, "high"), (70, "high"), (71, "critical")])
def test_risk_boundaries(score, level):
    assert detection.risk_level(score) == level


def us_ip(zone="America/Los_Angeles"):
    return {"ip": "203.0.113.1", "country": "US", "timezone": zone, "asn": "AS12345", "org": "Example",
            "ipType": "unknown", "sourceCount": 2, "multiSourceConsistent": True,
            "timezoneSources": [{"source": provider, "country": "US", "timezone": zone}
                                for provider in ("ipwho.is", "ipinfo.io")]}


@pytest.mark.parametrize("instant,offset", [(datetime(2026, 1, 1, tzinfo=timezone.utc), "UTC-08:00"),
                                         (datetime(2026, 7, 1, tzinfo=timezone.utc), "UTC-07:00")])
def test_auto_us_target_uses_ip_timezone_for_la_winter_and_summer(tmp_path, instant, offset):
    result = detect({"HOME": str(tmp_path), "TZ": "America/Los_Angeles", "LANG": "en_US.UTF-8"},
                    ip_intelligence=us_ip(), now=instant)
    assert result["targetTimezone"] == "America/Los_Angeles"
    assert result["timezoneSelection"]["source"] == "ip"
    assert by_id(result, "timezone")["score"] == by_id(result, "utc-offset")["score"] == 0
    assert by_id(result, "utc-offset")["value"] == offset
    assert by_id(result, "consistency")["contribution"] == 0


@pytest.mark.parametrize("zone,offset", [("America/Phoenix", "UTC-07:00"), ("Pacific/Honolulu", "UTC-10:00")])
@pytest.mark.parametrize("month", [1, 7])
def test_us_zones_without_dst_are_compared_using_their_own_rules(tmp_path, zone, offset, month):
    result = detect({"HOME": str(tmp_path), "TZ": zone, "LANG": "en_US.UTF-8"}, ip_intelligence=us_ip(zone),
                    now=datetime(2026, month, 1, tzinfo=timezone.utc))
    assert by_id(result, "utc-offset")["value"] == offset
    assert by_id(result, "utc-offset")["score"] == 0


def test_explicit_saved_profile_overrides_current_ip_target_for_diagnostics(tmp_path):
    environment = {"HOME": str(tmp_path), "TZ": "America/Los_Angeles", "LANG": "en_US.UTF-8"}
    result = detect(environment, timezone="America/Los_Angeles", ip_intelligence=us_ip("America/New_York"))
    same_exit = detect(environment, timezone="America/Los_Angeles", ip_intelligence=us_ip("America/Los_Angeles"))
    assert result["targetTimezone"] == "America/Los_Angeles"
    assert by_id(result, "timezone")["score"] == 0
    observation = by_id(result, "ip-timezone")
    assert observation["weight"] == observation["score"] == observation["contribution"] == 0
    assert observation["observationOnly"] is True
    assert "estimate(America/New_York)" in observation["value"] and "target(America/Los_Angeles)" in observation["value"]
    assert result["score"] == same_exit["score"] and result["riskLevel"] == same_exit["riskLevel"]
    assert result["status"] == "unknown" and result["accessVerified"] is False
    assert any("fucc on --region us" in item and "--timezone <IANA>" in item for item in result["recommendations"])
    assert not any(item["id"] == "ip-timezone" for item in same_exit["signals"])


def test_us_alias_does_not_create_a_false_timezone_mismatch(tmp_path):
    result = detect({"HOME": str(tmp_path), "TZ": "US/Pacific", "LANG": "en_US.UTF-8"}, ip_intelligence=us_ip())
    assert by_id(result, "timezone")["score"] == by_id(result, "consistency")["score"] == 0


def test_check_reuses_one_network_result_for_target_selection(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(detection, "fetch_ip_intelligence", lambda environment: calls.append(environment) or us_ip())
    monkeypatch.setattr(detection, "dns_signal", lambda resolver: detection.signal("dns", "DNS", "public", 8))
    monkeypatch.setattr(detection, "font_signal", lambda command: detection.signal("fonts", "Fonts", "none", 10))
    result = detection.check("us", {"HOME": str(tmp_path), "TZ": "America/Los_Angeles", "LANG": "en_US.UTF-8"})
    assert result["targetTimezone"] == "America/Los_Angeles" and len(calls) == 1


def test_foreign_ip_cannot_override_us_target_even_with_us_timezone(tmp_path):
    ip = us_ip()
    ip["country"] = "JP"
    for source in ip["timezoneSources"]:
        source["country"] = "JP"
    result = detect({"HOME": str(tmp_path), "TZ": "America/New_York", "LANG": "en_US.UTF-8"}, ip_intelligence=ip)
    assert result["targetTimezone"] == "America/New_York" and result["timezoneSelection"]["source"] == "default"
    assert "IP(JP) != target(US)" in by_id(result, "consistency")["value"]


def test_check_uses_supplied_saved_timezone_when_network_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(detection, "fetch_ip_intelligence", lambda environment: None)
    monkeypatch.setattr(detection, "dns_signal", lambda resolver: detection.signal("dns", "DNS", "public", 8))
    monkeypatch.setattr(detection, "font_signal", lambda command: detection.signal("fonts", "Fonts", "none", 10))
    result = detection.check("us", {"HOME": str(tmp_path), "TZ": "America/Phoenix", "LANG": "en_US.UTF-8"},
                             timezone="America/Phoenix")
    assert result["targetTimezone"] == "America/Phoenix"
    assert by_id(result, "timezone")["score"] == by_id(result, "utc-offset")["score"] == 0
    assert not any(item["id"] == "ip-timezone" for item in result["signals"])


@pytest.mark.parametrize("limitation", ["missing", "country_unknown", "foreign", "invalid_zone", "timezone_conflict", "source_disagreement", "proof_missing"])
def test_uncertain_ip_timezone_is_not_reported_as_located_or_as_a_target_mismatch(tmp_path, limitation):
    ip = us_ip("America/New_York")
    if limitation == "missing":
        ip = None
    elif limitation == "country_unknown":
        ip["timezoneSources"][1]["country"] = None
    elif limitation == "foreign":
        ip["country"] = "JP"
        for item in ip["timezoneSources"]:
            item["country"] = "JP"
    elif limitation == "invalid_zone":
        ip["timezoneSources"][1]["timezone"] = "Asia/Tokyo"
    elif limitation == "timezone_conflict":
        ip["timezoneSources"][1]["timezone"] = "America/Phoenix"
    elif limitation == "source_disagreement":
        ip["multiSourceConsistent"] = False
    elif limitation == "proof_missing":
        del ip["timezoneSources"]
    result = detect({"HOME": str(tmp_path), "TZ": "America/Los_Angeles", "LANG": "en_US.UTF-8"},
                    timezone="America/Los_Angeles", ip_intelligence=ip)
    assert not any(item["id"] == "ip-timezone" for item in result["signals"])
    assert not any("IP geolocation timezone estimate differs" in item for item in result["recommendations"])


def test_auto_target_does_not_add_a_saved_profile_mismatch_observation(tmp_path):
    result = detect({"HOME": str(tmp_path), "TZ": "America/Los_Angeles", "LANG": "en_US.UTF-8"},
                    ip_intelligence=us_ip("America/New_York"))
    assert result["targetTimezone"] == "America/New_York"
    assert not any(item["id"] == "ip-timezone" for item in result["signals"])
