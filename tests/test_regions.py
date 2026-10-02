import pytest

from fucc.errors import FuccError
from fucc.regions import US_TIMEZONES, get_target_region, normalize_us_timezone, resolve_timezone, target_environment


def evidence(*zones, countries=None, consistent=True, source_count=None):
    countries = countries or ["US"] * len(zones)
    return {"country": countries[0], "timezone": zones[0], "sourceCount": source_count or len(zones),
            "multiSourceConsistent": consistent,
            "timezoneSources": [{"source": f"provider-{index}", "country": country, "timezone": zone}
                                for index, (country, zone) in enumerate(zip(countries, zones))]}


@pytest.mark.parametrize("zone", sorted(US_TIMEZONES))
def test_all_us_primary_zones_are_supported_without_city_guessing(zone):
    assert normalize_us_timezone(zone) == zone
    assert get_target_region("us", zone)["timezone"] == zone


@pytest.mark.parametrize("value", [None, "", "Asia/Tokyo", "America/Toronto", "Europe/London", "PST", "UTC-08:00",
                                  "/usr/share/zoneinfo/America/Los_Angeles", "../America/New_York",
                                  "America/../Los_Angeles", "America/New_York\n", {"id": "America/New_York"}, 123])
def test_us_timezone_validation_rejects_foreign_zones_paths_and_offsets(value):
    assert normalize_us_timezone(value) is None


@pytest.mark.parametrize("alias,canonical", [("US/Pacific", "America/Los_Angeles"), ("US/Arizona", "America/Phoenix"),
                                          ("US/Hawaii", "Pacific/Honolulu"), ("US/Aleutian", "America/Adak"),
                                          ("US/East-Indiana", "America/Indiana/Indianapolis"),
                                          ("America/Louisville", "America/Kentucky/Louisville")])
def test_known_us_aliases_are_canonicalized(alias, canonical):
    assert normalize_us_timezone(alias) == canonical
    assert target_environment("us", alias) == {"TZ": canonical, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}


def test_explicit_override_precedes_ip_and_saved_zone():
    result = resolve_timezone("us", evidence("America/Los_Angeles"), "America/New_York", "US/Arizona")
    assert result["timezone"] == "America/Phoenix"
    assert result["source"] == "explicit"
    for zone in ("Asia/Tokyo", "PST8PDT", "../../etc/passwd"):
        with pytest.raises(FuccError) as error:
            resolve_timezone("us", explicit_timezone=zone)
        assert error.value.code == "INVALID_TIMEZONE" and error.value.exit_code == 10


@pytest.mark.parametrize("region,zone", [("eu", "Europe/London"), ("jp", "Asia/Tokyo"), ("sg", "Asia/Singapore")])
def test_non_us_regions_keep_their_fixed_profiles(region, zone):
    assert resolve_timezone(region, evidence("America/Los_Angeles"), saved_timezone="America/Chicago")["timezone"] == zone
    assert get_target_region(region, zone)["timezone"] == zone
    with pytest.raises(FuccError):
        get_target_region(region, "America/Los_Angeles")


def test_two_consistent_sources_select_la_even_if_only_secondary_has_timezone():
    result = resolve_timezone("us", evidence(None, "America/Los_Angeles"))
    assert result == {"timezone": "America/Los_Angeles", "source": "ip", "reason": "Consistent US IP sources and IANA timezone"}
    assert resolve_timezone("us", evidence("US/Pacific", "America/Los_Angeles"))["source"] == "ip"


def test_valid_single_source_is_usable_but_is_not_reported_as_multisource():
    result = resolve_timezone("us", evidence("Pacific/Honolulu"))
    assert result["timezone"] == "Pacific/Honolulu" and result["source"] == "ip"
    assert "single IP source" in result["reason"]
    legacy = {"country": "US", "timezone": "America/Phoenix", "sourceCount": 1}
    assert resolve_timezone("us", legacy)["timezone"] == "America/Phoenix"


@pytest.mark.parametrize("ip,reason", [
    (None, "IP lookup unavailable"),
    (evidence(None, None), "IP timezone unavailable"),
    (evidence("America/Los_Angeles", "America/Denver"), "IP source timezones disagree"),
    (evidence("America/Los_Angeles", "Asia/Tokyo"), "invalid US timezone"),
    (evidence("Asia/Tokyo", "Asia/Tokyo"), "invalid US timezone"),
    (evidence("America/Los_Angeles", "America/Los_Angeles", countries=["US", "JP"]), "countries do not all confirm US"),
    (evidence("America/Los_Angeles", "America/Los_Angeles", countries=["JP", "JP"]), "not confirmed as US"),
    (evidence("America/Los_Angeles", "America/Los_Angeles", countries=["US", None]), "countries do not all confirm US"),
    (evidence("America/Los_Angeles", "America/Los_Angeles", consistent=False), "IP sources disagree"),
    ({"country": "US", "timezone": "America/Los_Angeles", "sourceCount": 2, "multiSourceConsistent": True}, "evidence unavailable"),
    ({"country": "US", "region": "California", "city": "Los Angeles", "sourceCount": 1}, "IP timezone unavailable"),
])
def test_uncertain_provider_evidence_preserves_saved_timezone_then_default(ip, reason):
    saved = resolve_timezone("us", ip, saved_timezone="America/Chicago")
    assert saved["timezone"] == "America/Chicago" and saved["source"] == "saved"
    assert reason in saved["reason"]
    default = resolve_timezone("us", ip, saved_timezone="Asia/Tokyo")
    assert default["timezone"] == "America/New_York" and default["source"] == "default"


def test_corporate_exemption_cannot_hide_differing_ip_sources_for_timezone_selection():
    ip = {**evidence("America/Los_Angeles", "America/Los_Angeles"), "geolocationConsistent": False}
    assert resolve_timezone("us", ip, saved_timezone="America/Phoenix")["source"] == "saved"


@pytest.mark.parametrize("count", [0, -1, 1.5, True, "2"])
def test_invalid_source_count_is_not_trusted(count):
    ip = {**evidence("America/Los_Angeles"), "sourceCount": count}
    assert resolve_timezone("us", ip)["source"] == "default"
