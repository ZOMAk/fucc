"""Bounded HTTPS IP intelligence, optional corporate exemptions and credential redaction."""
import ipaddress
import json
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from .paths import xdg_directory

DATACENTERS = {"AS16509", "AS14618", "AS8075", "AS15169", "AS396982", "AS13335", "AS14061", "AS37963", "AS45090", "AS36351", "AS20473", "AS63949", "AS51167", "AS9009", "AS212238"}


def normalize_asn(value):
    # Accept an ASN token or the provider's "AS12345 Organization" form.
    # An embedded token or AS16509evil must not gain an ASN exemption.
    match = re.match(r"^\s*AS[0-9]+(?=\s|$)", value.upper()) if isinstance(value, str) else None
    return match.group().strip() if match else None


def allowlist(environment=None):
    env = os.environ if environment is None else environment
    paths = [xdg_directory(env, "XDG_CONFIG_HOME") / "fucc/corporate-allowlist.json"]
    if env.get("FUCC_CORPORATE_ALLOWLIST", "").startswith("/"):
        paths.insert(0, env["FUCC_CORPORATE_ALLOWLIST"])
    from pathlib import Path
    for path in paths:
        try:
            parsed = json.loads(Path(path).read_text())
            if not isinstance(parsed, dict) or not all(isinstance(parsed.get(key, []), list) for key in ("allowCidrs", "allowAsns")):
                continue
            return {"allowCidrs": [value.strip() for value in parsed.get("allowCidrs", []) if isinstance(value, str) and value.strip()],
                    "allowAsns": [normalized for value in parsed.get("allowAsns", []) if (normalized := normalize_asn(value)) is not None]}
        except (OSError, ValueError):
            continue
    return {"allowCidrs": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"], "allowAsns": []}


def corporate(ip=None, asn=None, environment=None):
    allowed = allowlist(environment)
    normalized = normalize_asn(asn)
    if normalized is not None and normalized in allowed["allowAsns"]:
        return True
    if ip:
        for cidr in allowed["allowCidrs"]:
            try:
                if ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                pass
    return False


def redact_url(value):
    try:
        url = urlsplit(value if "://" in value else "https://" + value)
        return ("***@" if url.username is not None or url.password is not None else "") + url.hostname if url.hostname else "(unparseable; hidden)"
    except ValueError:
        return "(unparseable; hidden)"


def fetch_json(url, environment=None):
    env = os.environ if environment is None else environment
    # Match Claude's documented HTTPS proxy precedence, without publishing proxy credentials.
    proxy = env.get("https_proxy") or env.get("HTTPS_PROXY") or env.get("http_proxy") or env.get("HTTP_PROXY")
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "FUCC/0.0.2"}), timeout=5) as response:
        payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("Response exceeded size limit")
        return json.loads(payload)


def source_payload(source, data):
    if not isinstance(data, dict) or data.get("success") is False:
        return None
    try:
        if not isinstance(data["ip"], str):
            return None
        address = ipaddress.ip_address(data["ip"])
    except (KeyError, ValueError, TypeError):
        return None
    if source == "ipwho":
        connection = data.get("connection") if isinstance(data.get("connection"), dict) else {}
        timezone = data.get("timezone") if isinstance(data.get("timezone"), dict) else {}
        result = {"ip": data["ip"], "country": data.get("country_code"), "region": data.get("region"), "city": data.get("city"),
                  "asn": normalize_asn(f'AS{connection["asn"]}') if connection.get("asn") is not None else None,
                  "org": connection.get("org"), "timezone": timezone.get("id")}
    else:
        result = {key: data.get(key) for key in ("ip", "country", "region", "city", "org", "timezone")}
        result["asn"] = normalize_asn(data.get("org"))
    result["ip"] = str(address)
    return {key: value if isinstance(value, str) else None for key, value in result.items()}


def merge_intelligence(primary, secondary=None, environment=None, *, source_names=None):
    consistent = True
    geolocation_consistent = True
    if secondary:
        country_match = not primary["country"] or not secondary["country"] or primary["country"].upper() == secondary["country"].upper()
        asn_match = not primary["asn"] or not secondary["asn"] or primary["asn"] == secondary["asn"]
        primary_ip = ipaddress.ip_address(primary["ip"])
        secondary_ip = ipaddress.ip_address(secondary["ip"])
        ip_match = primary_ip == secondary_ip or (primary_ip.version != secondary_ip.version and country_match and asn_match)
        consistent = country_match and asn_match and ip_match
        geolocation_consistent = consistent
        if corporate(asn=primary["asn"], environment=environment) or corporate(asn=secondary["asn"], environment=environment):
            consistent = True
    datacenter = primary["asn"] in DATACENTERS and not corporate(asn=primary["asn"], environment=environment)
    providers = [primary] + ([secondary] if secondary else [])
    names = source_names or ["primary", "secondary"]
    observations = [{"source": name, "country": provider.get("country"), "timezone": provider.get("timezone")}
                    for name, provider in zip(names, providers)]
    # Absence from a short ASN list cannot prove a residential connection.
    return {**primary, "timezone": primary.get("timezone") or (secondary.get("timezone") if secondary else None),
            "ipType": "datacenter" if datacenter else "unknown", "multiSourceConsistent": consistent,
            "geolocationConsistent": geolocation_consistent, "sourceCount": len(providers), "timezoneSources": observations}


def fetch_ip_intelligence(environment=None, fetcher=None):
    fetcher = fetcher or (lambda url: fetch_json(url, environment))
    urls = [("ipwho", "https://ipwho.is/?fields=ip,country,country_code,region,city,connection,timezone"),
            ("ipinfo", "https://ipinfo.io/json")]

    def probe(pair):
        source, url = pair
        try:
            return source_payload(source, fetcher(url))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(probe, urls)
    names = [name for name, payload in (("ipwho.is", first), ("ipinfo.io", second)) if payload is not None]
    result = merge_intelligence(first, second, environment, source_names=names) if first else merge_intelligence(second, environment=environment, source_names=names) if second else None
    if result is not None:
        result["sources"] = names
    return result
