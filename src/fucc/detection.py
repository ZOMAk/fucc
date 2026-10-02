"""Read-only Ubuntu environment observations; scores are not service-access decisions."""
import ipaddress
import locale
import math
import os
import re
import subprocess
from datetime import datetime, timezone as utc_timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .network import corporate, fetch_ip_intelligence, redact_url
from .regions import get_target_region, normalize_us_timezone, resolve_timezone

SENSITIVE_DOMAINS = {
    "api.openai-proxy.com", "openai.api.com", "api.chatgpt.com", "api.minimax.chat", "api.baichuan-ai.com",
    "open.bigmodel.cn", "chatglm.cn", "api.qianfan.baidu.com", "wenxin.baidu.com", "dashscope.aliyuncs.com",
    "tongyi.aliyun.com", "api.hunyuan.cloud.tencent.com", "hunyuan.tencent.com", "api.sensenova.cn",
    "platform.moonshot.cn", "api.moonshot.cn", "api.doubao.com", "www.doubao.com", "api.deepseek.com",
    "deepseek.com", "api.zhipuai.cn", "openai.azure.com", "api.openai-proxy.org", "openai-proxy.com",
    "chatgpt-proxy.com", "claude-proxy.com", "anthropic-proxy.com", "api.w2w.qzz.io", "api.aigc369.com",
    "api.chatanywhere.org", "openai.api2d.net", "api.caipua.com", "api.xiamoai.top",
}
EUROPE = set("AL AD AT BY BE BA BG HR CY CZ DK EE FI FR DE GR HU IS IE IT XK LV LI LT LU MT MD MC ME NL MK NO PL PT RO RU SM RS SK SI ES SE CH TR UA GB VA".split())


def signal(identifier, label, value, weight, score=0, source="system", availability="available", risk=None):
    return {"id": identifier, "label": label, "value": value, "score": score, "weight": weight,
            "contribution": math.floor(score * weight + 0.5) if availability == "available" else 0, "source": source,
            "risk": risk or ("low" if score == 0 else "medium" if score < 1 else "high"), "availability": availability}


def system_timezone():
    try:
        path = Path("/etc/localtime").resolve()
        return str(path).split("/zoneinfo/", 1)[1]
    except (OSError, IndexError):
        try:
            return subprocess.run(["/usr/bin/timedatectl", "show", "--property=Timezone", "--value"], text=True,
                                  capture_output=True, timeout=5, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None


def process_timezone(environment=None):
    env = os.environ if environment is None else environment
    value = env.get("TZ")
    if value:
        value = value.removeprefix(":")
        if value.startswith("/usr/share/zoneinfo/"):
            value = value.removeprefix("/usr/share/zoneinfo/")
    else:
        value = system_timezone()
    if value:
        try:
            ZoneInfo(value)
            return value
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return None


def dns_signal(resolver=None):
    try:
        if resolver:
            address = resolver("api.anthropic.com")
        else:
            result = subprocess.run(["/usr/bin/getent", "ahosts", "api.anthropic.com"], text=True,
                                    capture_output=True, timeout=5, check=True)
            address = result.stdout.split()[0]
        ip = ipaddress.ip_address(address)
        fake = ip.version == 4 and ip in ipaddress.ip_network("198.18.0.0/16")
        suspicious = not fake and (ip.is_private or ip.is_loopback or ip.is_link_local or ip in ipaddress.ip_network("100.64.0.0/10"))
        note = "Clash fake-ip" if fake else "private or special address; inspect DNS/proxy" if suspicious else "public address"
        return signal("dns", "DNS 配置", f"api.anthropic.com -> {ip} ({note})", 8, 0.75 if suspicious else 0, "network")
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return signal("dns", "DNS 配置", "DNS lookup unavailable or timed out", 8, source="network", availability="limited", risk="medium")


def font_signal(command=None):
    try:
        text = command() if command else subprocess.run(["fc-list", ":lang=zh", "family"], text=True, capture_output=True, timeout=5, check=True).stdout
        families = {line.strip() for line in text.splitlines() if line.strip()}
        return signal("fonts", "系统字体", f"{len(families)} Chinese font families; observation only", 10)
    except (OSError, subprocess.SubprocessError):
        return signal("fonts", "系统字体", "fontconfig unavailable", 10, availability="limited", risk="medium")


def calculate_score(signals):
    available = [item for item in signals if item.get("availability", "available") == "available"]
    weight = sum(item["weight"] for item in available)
    return min(100, math.floor(sum(item["contribution"] for item in available) / weight * 100 + 0.5)) if weight else 0


def risk_level(score):
    return "critical" if score >= 71 else "high" if score >= 51 else "medium" if score >= 21 else "low"


def _locale_name(value):
    return re.split(r"[.@]", value, maxsplit=1)[0].replace("-", "_").lower()


def detect(region, environment=None, ip_intelligence=None, *, resolver=None, fonts=None, now=None, python_locale=None, timezone=None):
    env = os.environ if environment is None else environment
    timezone_selection = resolve_timezone(region, ip_intelligence, explicit_timezone=timezone)
    target = get_target_region(region, timezone_selection["timezone"])
    zone = process_timezone(env)
    comparable_zone = normalize_us_timezone(zone) or zone if region == "us" else zone
    language = env.get("LC_ALL") or env.get("LANG") or "C"
    try:
        native_locale = python_locale if python_locale is not None else locale.setlocale(locale.LC_CTYPE)
    except locale.Error:
        native_locale = None
    timezone_score = 0 if comparable_zone == target["timezone"] else 1 if zone in {"Asia/Shanghai", "Asia/Urumqi", "Asia/Chongqing"} else 0.5
    target_locale = _locale_name(target["lang"])
    language_name, native_name = _locale_name(language), _locale_name(native_locale) if native_locale else None
    language_score = 1 if language_name in {"zh_cn", "zh_hans"} else 0 if target_locale == language_name else 0.5
    locale_score = 1 if native_name in {"zh_cn", "zh_hans"} else 0 if target_locale == native_name else 0.5
    signals = [signal("timezone", "进程时区", zone or "Unknown process timezone", 25, timezone_score,
                      availability="available" if zone else "limited"),
               signal("language", "进程语言", language, 20, language_score),
               signal("locale", "Python Locale", native_locale or "Unknown Python Locale", 6, locale_score,
                      availability="available" if native_locale else "limited")]
    inconsistencies = []
    if zone and comparable_zone != target["timezone"]:
        inconsistencies.append(f"timezone({zone}) != target({target['timezone']})")
    if native_name and target_locale != native_name:
        inconsistencies.append(f"Locale({native_locale}) != target({target['lang']})")
    ip = ip_intelligence
    if ip and ip.get("country"):
        mismatch = ip["country"].upper() not in EUROPE if region == "eu" else ip["country"].upper() != {"us": "US", "jp": "JP", "sg": "SG"}[region]
        if mismatch and not corporate(ip=ip.get("ip"), asn=ip.get("asn"), environment=env):
            inconsistencies.append(f'IP({ip["country"]}) != target({region.upper()})')
    score = 1 if len(inconsistencies) >= 2 else 0.5 if inconsistencies else 0
    signals.append(signal("consistency", "信号一致性", "; ".join(inconsistencies) or ("Consistent" if ip else "Local consistent; IP unavailable"), 15, score, "combined"))
    signals.extend([font_signal(fonts), dns_signal(resolver)])
    base = env.get("ANTHROPIC_BASE_URL")
    sensitive = False
    if base:
        try:
            host = urlsplit(base).hostname or ""
            sensitive = any(host == domain or host.endswith("." + domain) for domain in SENSITIVE_DOMAINS)
        except ValueError:
            pass
    signals.append(signal("base-url", "BASE_URL 域名", redact_url(base) if base else "(unset)", 8, int(sensitive)))
    keys = [key for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy") if env.get(key)]
    signals.append(signal("proxy-env", "代理环境", "Configured: " + ", ".join(keys) if keys else "No proxy environment variables", 6))
    instant = now or datetime.now(utc_timezone.utc)
    expected = instant.astimezone(ZoneInfo(target["timezone"])).utcoffset()
    actual = instant.astimezone(ZoneInfo(zone)).utcoffset() if zone else None
    if actual is None:
        signals.append(signal("utc-offset", "进程 UTC 偏移", "Unavailable", 4, availability="limited", risk="medium"))
    else:
        minutes = int(actual.total_seconds() // 60)
        offset = f'UTC{"-" if minutes < 0 else "+"}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}'
        signals.append(signal("utc-offset", "进程 UTC 偏移", offset, 4, int(actual != expected), risk="low" if actual == expected else "medium"))
    if not ip or not ip.get("ip") or not ip.get("country"):
        signals.append(signal("ip-intelligence", "IP 情报", "Exit IP/country unavailable", 0, source="network", availability="limited", risk="medium"))
    else:
        if ip["ipType"] == "datacenter":
            signals.append(signal("ip-datacenter", "数据中心 IP", f'{ip["asn"]} ({ip["org"]})', 13, 1, "network"))
        if ip["sourceCount"] < 2:
            signals.append(signal("ip-multi-source", "多源一致性", "Only one IP source available", 15, source="network", availability="limited", risk="medium"))
        elif not ip["multiSourceConsistent"]:
            signals.append(signal("ip-multi-source", "多源一致性", "IP sources disagree", 15, 1, "network"))
    ip_timezone_mismatch = False
    if region == "us" and timezone_selection["source"] == "explicit":
        # A saved or explicit profile remains the diagnostic target. Provider
        # geolocation can suggest an update without adding an account-risk score.
        ip_timezone = resolve_timezone(region, ip)
        if ip_timezone["source"] == "ip" and ip_timezone["timezone"] != target["timezone"]:
            ip_timezone_mismatch = True
            signals.append({**signal("ip-timezone", "IP 时区观察",
                                     f"IP timezone estimate({ip_timezone['timezone']}) != target({target['timezone']})",
                                     0, source="network"), "observationOnly": True})
    total = calculate_score(signals)
    recommendations = []
    for item in signals:
        if item["availability"] == "limited":
            recommendations.append(f'{item["label"]}: resolve the detection limit and retry fucc check.')
    if any(item["availability"] == "available" and item["contribution"] > 0 and item["id"] in {"timezone", "language", "locale", "utc-offset"} for item in signals):
        recommendations.append("Run fucc on for the selected region; load env.sh or open a new Bash terminal and restart applications.")
    if ip and any(value.startswith("IP(") for value in inconsistencies):
        recommendations.append("Check the selected region and network exit; FUCC does not change router, VPN or proxy routing.")
    if ip_timezone_mismatch:
        recommendations.append("The IP geolocation timezone estimate differs from the saved/explicit target. Re-run fucc on --region us to match this exit, or use fucc on --region us --timezone <IANA> after verifying the timezone.")
    if sensitive:
        recommendations.append("Verify ANTHROPIC_BASE_URL and its service provider; FUCC does not change this variable.")
    if any(item["id"] == "ip-datacenter" for item in signals):
        recommendations.append("The exit ASN matches a datacenter heuristic; verify the actual network exit.")
    if any(item["id"] == "ip-multi-source" and item["contribution"] for item in signals):
        recommendations.append("IP sources disagree; inspect routing or node changes and retry.")
    if signals[5]["contribution"]:
        recommendations.append("Inspect DNS and proxy interception; a private address alone does not prove DNS pollution.")
    if not recommendations:
        recommendations.append("Completed checks found no scored inconsistencies; access and account status are not verified.")
    return {"score": total, "riskLevel": risk_level(total), "status": "unknown", "region": "auto", "matchedRegion": None,
            "signals": signals, "ipIntelligence": ip, "recommendations": recommendations,
            "diagnosticRuntime": "python", "accessVerified": False, "targetTimezone": target["timezone"],
            "timezoneSelection": timezone_selection}


def check(region, environment=None, timezone=None):
    return detect(region, environment, fetch_ip_intelligence(environment), timezone=timezone)
