"""A small JSON/JSONC parser whose edits retain untouched source spans.

Only inserted or replaced values are serialized. Comments, number spellings,
duplicate unrelated properties, and formatting elsewhere remain as written.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

MISSING = object()
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


@dataclass
class Token:
    kind: str
    start: int
    end: int
    value: Any = None


@dataclass
class Node:
    kind: str
    start: int
    end: int
    value: Any
    children: list["Node"] = field(default_factory=list)
    key: str | None = None

    def properties(self, key: str) -> list["Node"]:
        return [child for child in self.children if child.kind == "property" and child.key == key]


def scan(text: str, *, comments: bool = True) -> list[Token]:
    tokens = []
    offset = 0
    while offset < len(text):
        character = text[offset]
        if character in " \t\r\n":
            offset += 1
            continue
        start = offset
        if character in "{}[]:,":
            offset += 1
            tokens.append(Token(character, start, offset))
        elif character == '"':
            offset += 1
            while offset < len(text):
                if text[offset] == "\\":
                    offset += 2
                elif text[offset] == '"':
                    offset += 1
                    break
                else:
                    offset += 1
            try:
                value = json.loads(text[start:offset])
            except (ValueError, TypeError) as error:
                raise ValueError(f"Invalid JSON string at offset {start}") from error
            tokens.append(Token("string", start, offset, value))
        elif text.startswith("//", offset):
            if not comments:
                raise ValueError(f"JSON comments are forbidden at offset {start}")
            end = text.find("\n", offset)
            offset = len(text) if end < 0 else end
            tokens.append(Token("comment", start, offset, text[start:offset]))
        elif text.startswith("/*", offset):
            if not comments:
                raise ValueError(f"JSON comments are forbidden at offset {start}")
            end = text.find("*/", offset + 2)
            if end < 0:
                raise ValueError(f"Unterminated comment at offset {start}")
            offset = end + 2
            tokens.append(Token("comment", start, offset, text[start:offset]))
        elif character == "-" or character.isdigit():
            match = _NUMBER.match(text, offset)
            if match is None:
                raise ValueError(f"Invalid number at offset {start}")
            offset = match.end()
            tokens.append(Token("number", start, offset, json.loads(match.group())))
        else:
            for literal, value in (("true", True), ("false", False), ("null", None)):
                if text.startswith(literal, offset):
                    offset += len(literal)
                    tokens.append(Token("literal", start, offset, value))
                    break
            else:
                raise ValueError(f"Invalid JSON token at offset {start}")
    tokens.append(Token("eof", len(text), len(text)))
    return tokens


def parse_document(text: str, *, comments: bool = True, trailing: bool = True) -> Node:
    tokens = [token for token in scan(text, comments=comments) if token.kind != "comment"]
    cursor = 0

    def take(kind: str) -> Token:
        nonlocal cursor
        token = tokens[cursor]
        if token.kind != kind:
            raise ValueError(f"Expected {kind} at offset {token.start}")
        cursor += 1
        return token

    def value() -> Node:
        nonlocal cursor
        token = tokens[cursor]
        if token.kind in ("string", "number", "literal"):
            cursor += 1
            return Node(token.kind, token.start, token.end, token.value)
        if token.kind not in ("{", "["):
            raise ValueError(f"Expected value at offset {token.start}")
        opening = token.kind
        closing = "}" if opening == "{" else "]"
        cursor += 1
        children = []
        output = {} if opening == "{" else []
        while tokens[cursor].kind != closing:
            if opening == "{":
                key = take("string")
                take(":")
                child = value()
                children.append(Node("property", key.start, child.end, child.value, [child], key.value))
                output[key.value] = child.value
            else:
                child = value()
                children.append(child)
                output.append(child.value)
            if tokens[cursor].kind == closing:
                break
            take(",")
            if tokens[cursor].kind == closing and not trailing:
                raise ValueError(f"Trailing comma at offset {tokens[cursor].start}")
        end = take(closing).end
        return Node("object" if opening == "{" else "array", token.start, end, output, children)

    root = value()
    take("eof")
    return root


def parse(text: str, *, comments: bool = True, trailing: bool = True) -> Any:
    return parse_document(text, comments=comments, trailing=trailing).value


def _apply(text: str, replacements: list[tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _indent(text: str, node: Node) -> str:
    line = text[text.rfind("\n", 0, node.start) + 1:node.start]
    return re.match(r"[ \t]*", line).group()


def _serialized(value: Any, indent: str) -> str:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    return raw.replace("\n", "\n" + indent)


def _child(parent: Node, segment: str | int) -> Node | None:
    if isinstance(segment, str):
        if parent.kind != "object":
            raise ValueError("JSON edit path requires an object")
        rows = parent.properties(segment)
        return rows[-1] if rows else None
    if parent.kind != "array":
        raise ValueError("JSON edit path requires an array")
    return parent.children[segment] if 0 <= segment < len(parent.children) else None


def _insert(text: str, parent: Node, segment: str | int, value: Any) -> str:
    if parent.kind == "object" and not isinstance(segment, str):
        raise ValueError("Object edit requires a property name")
    if parent.kind == "array" and (not isinstance(segment, int) or segment not in (-1, len(parent.children))):
        raise ValueError("Array edit must append a row")
    closing = parent.end - 1
    indent = _indent(text, parent) + "  "
    eol = "\r\n" if "\r\n" in text else "\n"
    contents = (json.dumps(segment, ensure_ascii=False) + ": " if parent.kind == "object" else "") + _serialized(value, indent)
    contents = contents.replace("\n", eol) if eol != "\n" else contents
    changes = []
    trailing = False
    separator = ""
    if parent.children:
        last = parent.children[-1]
        trailing = any(token.kind == "," for token in scan(text[last.end:closing]))
        if not trailing:
            if last.end == closing:
                separator = ","
            else:
                changes.append((last.end, last.end, ","))
    # Insert at the closing delimiter so comments after the previous row survive.
    prefix = "" if closing > 0 and text[closing - 1] == "\n" else eol
    changes.append((closing, closing, separator + prefix + indent + contents + ("," if trailing else "") + eol + _indent(text, parent)))
    return _apply(text, changes)


def _delete(text: str, parent: Node, child: Node) -> str:
    index = parent.children.index(child)
    changes = [(child.start, child.end, "")]
    if index + 1 < len(parent.children):
        start, end = child.end, parent.children[index + 1].start
    elif index > 0:
        start, end = parent.children[index - 1].end, child.start
    else:
        start, end = child.end, parent.end - 1
    commas = [token for token in scan(text[start:end]) if token.kind == ","]
    if commas:
        comma = commas[0]
        changes.append((start + comma.start, start + comma.end, ""))
    # A sole row with a trailing comma must lose that comma too. For a final
    # row following another row, its trailing comma can become the previous
    # row's trailing comma and is valid JSONC.
    return _apply(text, changes)


def edit(text: str, path: list[str | int], value: Any = MISSING) -> str:
    root = parse_document(text)
    before_comments = [token.value for token in scan(text) if token.kind == "comment"]
    if not path:
        if value is MISSING:
            raise ValueError("Cannot remove the JSON document")
        updated = _apply(text, [(root.start, root.end, _serialized(value, ""))])
    else:
        parent = root
        updated = None
        for index, segment in enumerate(path[:-1]):
            child = _child(parent, segment)
            if child is None:
                if value is MISSING:
                    return text
                nested = value
                for key in reversed(path[index + 1:]):
                    nested = {key: nested} if isinstance(key, str) else [nested]
                updated = _insert(text, parent, segment, nested)
                break
            parent = child.children[0] if child.kind == "property" else child
        if updated is None:
            child = _child(parent, path[-1])
            if child is None:
                return text if value is MISSING else _insert(text, parent, path[-1], value)
            if value is MISSING:
                updated = _delete(text, parent, child)
            else:
                target = child.children[0] if child.kind == "property" else child
                updated = _apply(text, [(target.start, target.end, _serialized(value, _indent(text, child)))])
    retained = Counter(token.value for token in scan(updated) if token.kind == "comment")
    missing = []
    for comment in before_comments:
        if retained[comment]:
            retained[comment] -= 1
        else:
            missing.append(comment)
    return "".join(comment + "\n" for comment in missing) + updated
