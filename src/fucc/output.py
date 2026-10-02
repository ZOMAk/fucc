"""Plain text ASCII tables with display-width-aware CJK wrapping."""
import json
import re
import shutil
import unicodedata
from itertools import zip_longest

from wcwidth import wcwidth, wcswidth

from . import __version__

ANSI = re.compile(
    r"(?:\x1b\]|\x9d)[\s\S]*?(?:\x07|\x1b\\|\x9c|$)"
    r"|\x1b[P^_X][\s\S]*?(?:\x1b\\|\x9c|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[0-~]"
)


def clean(value):
    text = ANSI.sub("", str(value).replace("\r\n", "\n").replace("\r", "\n")).replace("\t", " ")
    return "".join(character for character in text if character == "\n" or
                   (unicodedata.category(character) not in {"Cc", "Cf", "So", "Cs"}
                    and not 0x2500 <= ord(character) <= 0x27BF and character not in "\ufe0f\ufe0e\u20e3"))


def width(text):
    return max(0, wcswidth(clean(text)))


def wrap(value, columns):
    if type(columns) is not int or columns < 2:
        raise ValueError("Text width must be at least 2 columns")
    lines = []
    for paragraph in clean(value).split("\n"):
        line, size = "", 0
        for character in paragraph:
            span = max(0, wcwidth(character))
            if size + span > columns and line:
                lines.append(line)
                line, size = "", 0
            if span <= columns:
                line += character
                size += span
        lines.append(line)
    return lines or [""]


def table(title, headers, rows, columns=None, weights=None):
    requested = columns if columns is not None else shutil.get_terminal_size((96, 24)).columns
    if type(requested) is not int or requested < 1:
        raise ValueError("Terminal width must be a positive integer")
    total = max(24, min(96, requested))
    count = len(headers)
    room = total - 3 * count - 1
    if not count or room < 2 * count:
        raise ValueError("Too many table columns for the terminal width")
    ratios = [max(1, weights[index]) if weights and index < len(weights) else 1 for index in range(count)]
    widths = [max(2, int(room * ratio / sum(ratios))) for ratio in ratios]
    while sum(widths) > room:
        index = max(range(count), key=lambda index: widths[index])
        widths[index] -= 1
    while sum(widths) < room:
        widths[-1] += 1
    border = "+" + "+".join("-" * (size + 2) for size in widths) + "+"
    whole_border = "+" + "-" * (total - 2) + "+"
    result = [whole_border]
    for line in wrap(title, total - 4):
        result.append("| " + line + " " * (total - 4 - width(line)) + " |")
    result.append(border)

    def emit(values):
        cells = [wrap(values[index] if index < len(values) else "", size) for index, size in enumerate(widths)]
        for line in zip_longest(*cells, fillvalue=""):
            result.append("| " + " | ".join(value + " " * (size - width(value))
                                           for value, size in zip(line, widths)) + " |")

    emit(headers)
    result.append("+" + "+".join("=" * (size + 2) for size in widths) + "+")
    for row in rows:
        emit(["N/A" if item is None else item for item in row])
    result.append(border)
    return "\n".join(result)


def summary(title, rows, columns=None):
    print(table(title, ["Field", "Value"], rows, columns, [1, 3]))


def json_output(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


def display(value):
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def check_report(response, region):
    signals = response["signals"]
    available = [signal for signal in signals if signal.get("availability", "available") == "available"]
    limited = sum(signal.get("availability") == "limited" for signal in signals)
    summary(f"FUCC {__version__} | Python CLI | Target: {region}", [
        ("Score", f'{response["score"]}/100'), ("Risk", response["riskLevel"].upper()),
        ("Coverage", f"{len(available)}/{len(signals)} completed; {limited} limited"),
        ("Target timezone", response.get("targetTimezone", "unknown")),
        ("Timezone selection", display(response.get("timezoneSelection"))),
        ("Access", "Not verified by this diagnostic"),
    ])
    if response["ipIntelligence"]:
        print()
        ip_report(response["ipIntelligence"])
    print()
    ordered = sorted(signals, key=lambda signal: -signal["contribution"])
    rows = [(signal["label"], signal["value"], "LIMITED" if signal.get("availability") == "limited" else "SKIPPED" if signal.get("availability") == "unsupported" else signal["risk"].upper(),
             "N/A" if signal.get("availability", "available") != "available" else f'+{signal["contribution"]}') for signal in ordered]
    columns = shutil.get_terminal_size((96, 24)).columns
    if columns < 60:
        for label, value, state, points in rows:
            summary(label, [("Value", value), ("State", state), ("Points", points)])
    else:
        print(table("Detection signals", ["Signal", "Value", "State", "Points"], rows, weights=[2, 5, 1.1, 1]))
    print()
    summary("Recommendations", [(str(index + 1), action) for index, action in enumerate(response["recommendations"])])


def ip_report(ip):
    count = ip["sourceCount"]
    summary("Network exit", [("IP", ip["ip"]), ("Location", " / ".join(filter(None, [ip["country"], ip["region"], ip["city"]]))),
             ("Organization", f'{ip["org"] or "N/A"} ({ip["asn"] or "N/A"})'), ("Timezone", ip["timezone"]),
             ("IP type", ip["ipType"].upper()),
             ("Sources", f'{"UNVERIFIED" if count < 2 else "CONSISTENT" if ip["multiSourceConsistent"] else "INCONSISTENT"} ({count})')])
