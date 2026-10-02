"""Pure region data shared by environment, browser and diagnostic adapters."""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import FuccError

TARGET_REGIONS = {
    "us": {"code": "us", "name": "United States", "timezone": "America/New_York", "lang": "en_US.UTF-8", "lcAll": "en_US.UTF-8"},
    "eu": {"code": "eu", "name": "Europe", "timezone": "Europe/London", "lang": "en_GB.UTF-8", "lcAll": "en_GB.UTF-8"},
    "jp": {"code": "jp", "name": "Japan", "timezone": "Asia/Tokyo", "lang": "ja_JP.UTF-8", "lcAll": "ja_JP.UTF-8"},
    "sg": {"code": "sg", "name": "Singapore", "timezone": "Asia/Singapore", "lang": "en_SG.UTF-8", "lcAll": "en_SG.UTF-8"},
}

# US entries from IANA zone.tab, distributed by Ubuntu tzdata. Keeping an
# explicit allowlist prevents provider strings from becoming arbitrary paths
# or foreign zones while preserving US regions with different DST rules.
US_TIMEZONES = frozenset({
    "America/New_York", "America/Detroit", "America/Kentucky/Louisville", "America/Kentucky/Monticello",
    "America/Indiana/Indianapolis", "America/Indiana/Vincennes", "America/Indiana/Winamac",
    "America/Indiana/Marengo", "America/Indiana/Petersburg", "America/Indiana/Vevay", "America/Chicago",
    "America/Indiana/Tell_City", "America/Indiana/Knox", "America/Menominee", "America/North_Dakota/Center",
    "America/North_Dakota/New_Salem", "America/North_Dakota/Beulah", "America/Denver", "America/Boise",
    "America/Phoenix", "America/Los_Angeles", "America/Anchorage", "America/Juneau", "America/Sitka",
    "America/Metlakatla", "America/Yakutat", "America/Nome", "America/Adak", "Pacific/Honolulu",
})
US_TIMEZONE_ALIASES = {
    "US/Eastern": "America/New_York", "US/Central": "America/Chicago", "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles", "US/Arizona": "America/Phoenix", "US/Hawaii": "Pacific/Honolulu",
    "US/Alaska": "America/Anchorage", "US/Aleutian": "America/Adak", "US/Michigan": "America/Detroit",
    "US/East-Indiana": "America/Indiana/Indianapolis", "US/Indiana-Starke": "America/Indiana/Knox",
    "America/Indianapolis": "America/Indiana/Indianapolis", "America/Fort_Wayne": "America/Indiana/Indianapolis",
    "America/Louisville": "America/Kentucky/Louisville", "America/Knox_IN": "America/Indiana/Knox",
    "America/Atka": "America/Adak", "America/Shiprock": "America/Denver", "Navajo": "America/Denver",
}


def normalize_us_timezone(value):
    """Return a loadable canonical US IANA zone, never a path or UTC offset."""
    if not isinstance(value, str):
        return None
    value = US_TIMEZONE_ALIASES.get(value, value)
    if value not in US_TIMEZONES:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return value


def get_target_region(code, timezone=None):
    if not isinstance(code, str) or code not in TARGET_REGIONS:
        raise FuccError("Invalid region; expected us, eu, jp or sg", "INVALID_REGION", 10)
    target = dict(TARGET_REGIONS[code])
    if timezone is not None:
        resolved = normalize_us_timezone(timezone) if code == "us" else timezone if timezone == target["timezone"] else None
        if resolved is None:
            raise FuccError("Invalid timezone for selected region", "INVALID_TIMEZONE", 10)
        target["timezone"] = resolved
    return target


def resolve_region(explicit=None, active=None, preferred=None):
    return get_target_region(next((value for value in (explicit, active, preferred) if value is not None), "us"))


def target_environment(region, timezone=None):
    target = get_target_region(region, timezone)
    return {"TZ": target["timezone"], "LANG": target["lang"], "LC_ALL": target["lcAll"]}


def resolve_timezone(region, ip_intelligence=None, saved_timezone=None, explicit_timezone=None):
    """Choose a US zone from provider evidence, with a stable offline fallback.

    State/city labels are insufficient because state boundaries and time zone
    boundaries differ. Only validated IANA identifiers enter configuration.
    """
    target = get_target_region(region, explicit_timezone)
    if explicit_timezone is not None:
        return {"timezone": target["timezone"], "source": "explicit", "reason": "Explicit timezone override"}
    if region != "us":
        return {"timezone": target["timezone"], "source": "default", "reason": "Fixed timezone for selected region"}

    def fallback(reason):
        saved = normalize_us_timezone(saved_timezone)
        return {"timezone": saved or target["timezone"], "source": "saved" if saved else "default",
                "reason": reason + ("; retaining saved US timezone" if saved else "; using US default")}

    ip = ip_intelligence
    if not isinstance(ip, dict):
        return fallback("IP lookup unavailable")
    if not isinstance(ip.get("country"), str) or ip["country"].upper() != "US":
        return fallback("Exit country is not confirmed as US")
    observations = ip.get("timezoneSources")
    count = ip.get("sourceCount")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return fallback("IP source count unavailable")
    if observations is None and count == 1:
        # Retain compatibility with single-provider integrations predating the
        # per-source field; a merged multi-provider value is never sufficient.
        observations = [{"country": ip.get("country"), "timezone": ip.get("timezone")}]
    if not isinstance(observations, list) or len(observations) != count:
        return fallback("Per-source timezone evidence unavailable")
    if count > 1 and (ip.get("multiSourceConsistent") is not True or ip.get("geolocationConsistent", True) is not True):
        return fallback("IP sources disagree")
    zones = set()
    for observation in observations:
        if not isinstance(observation, dict) or not isinstance(observation.get("country"), str) or observation["country"].upper() != "US":
            return fallback("IP source countries do not all confirm US")
        value = observation.get("timezone")
        if value in (None, ""):
            continue
        normalized = normalize_us_timezone(value)
        if normalized is None:
            return fallback("IP source returned an invalid US timezone")
        zones.add(normalized)
    if not zones:
        return fallback("IP timezone unavailable")
    if len(zones) != 1:
        return fallback("IP source timezones disagree")
    reason = "US IANA timezone reported by a single IP source" if count == 1 else "Consistent US IP sources and IANA timezone"
    return {"timezone": zones.pop(), "source": "ip", "reason": reason}
