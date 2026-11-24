#!/usr/bin/env python3
"""A strict YAML subset reader, so the workflow files can be asserted on in stdlib.

This project has no third-party dependencies and never will, so ``PyYAML`` is not
an option and a workflow file would otherwise be verifiable only by eye or by
pushing it to GitHub and reading the logs. A hand-written parser is only worth
having if it refuses to guess: anything this module does not understand raises
``UnsupportedYaml`` instead of being dropped, because a silently skipped line is
a detail the tests would then never check.

The subset covers what ``.github/workflows/*.yml`` in this repo actually uses --
block mappings, block sequences, block scalars (``|`` and ``>`` with chomping),
flow sequences of scalars, quoted and plain scalars, and ``#`` comments. That is
deliberately not "most of YAML": anchors, multiline flow collections, multiple
documents and non-string mapping keys all raise.

Keys are always strings, so ``on:`` stays ``"on"`` rather than becoming the
boolean ``True`` the way YAML 1.1 loaders treat it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_INDENT = "  "
_BLOCK_SCALARS = ("|", "|-", "|+", ">", ">-", ">+")
_QUOTES = ("'", '"')


class UnsupportedYaml(ValueError):
    """The reader met a construct it will not guess at."""


def load(path: Path) -> Any:
    return loads(Path(path).read_text(encoding="utf-8"))


def loads(text: str) -> Any:
    lines = text.splitlines()
    index = _skip(lines, 0)
    if index >= len(lines):
        return None
    indent = _indent_of(lines[index])
    if indent != 0:
        raise UnsupportedYaml(f"line {index + 1}: document does not start at column 0")
    value, index = _parse_node(lines, index, indent)
    remaining = _skip(lines, index)
    if remaining < len(lines):
        raise UnsupportedYaml(
            f"line {remaining + 1}: trailing content at the document root"
        )
    return value


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _skip(lines: list, index: int) -> int:
    """Advance past blank lines and whole-line comments."""
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            return index
        index += 1
    return index


def _parse_node(lines: list, index: int, indent: int) -> tuple:
    content = lines[index].strip()
    if content.startswith("- "):
        return _parse_seq(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_map(lines: list, index: int, indent: int) -> tuple:
    result: dict = {}
    while True:
        index = _skip(lines, index)
        if index >= len(lines):
            return result, index
        line = lines[index]
        if _indent_of(line) < indent:
            return result, index
        if _indent_of(line) > indent:
            raise UnsupportedYaml(f"line {index + 1}: unexpected indent in a mapping")
        content = line.strip()
        if content.startswith("- "):
            raise UnsupportedYaml(
                f"line {index + 1}: a sequence item at a mapping's own indent"
            )
        key, rest = _split_key(content, index)
        if key in result:
            raise UnsupportedYaml(f"line {index + 1}: duplicate key {key!r}")
        if rest in _BLOCK_SCALARS:
            result[key], index = _block_scalar(lines, index + 1, indent, rest, index)
        elif rest == "":
            child = _skip(lines, index + 1)
            if child < len(lines) and _indent_of(lines[child]) > indent:
                result[key], index = _parse_node(lines, child, _indent_of(lines[child]))
            else:
                result[key], index = None, index + 1
        else:
            result[key], index = _scalar(rest, index), index + 1


def _parse_seq(lines: list, index: int, indent: int) -> tuple:
    items: list = []
    while True:
        index = _skip(lines, index)
        if index >= len(lines):
            return items, index
        line = lines[index]
        if _indent_of(line) < indent:
            return items, index
        if _indent_of(line) > indent:
            raise UnsupportedYaml(f"line {index + 1}: unexpected indent in a sequence")
        content = line.strip()
        if not content.startswith("- "):
            return items, index
        rest = content[2:]
        if rest in _BLOCK_SCALARS:
            value, index = _block_scalar(lines, index + 1, indent, rest, index)
            items.append(value)
        elif _has_key(rest):
            # `- key: value` is a mapping whose first key sits two columns in.
            # Rewrite the dash as indentation so the mapping parser sees an
            # ordinary block, then let it consume the following keys itself.
            lines[index] = _INDENT + (" " * indent) + rest
            value, index = _parse_map(lines, index, indent + 2)
            items.append(value)
        else:
            items.append(_scalar(rest, index))
            index += 1


def _has_key(content: str) -> bool:
    try:
        _split_key(content, 0)
    except UnsupportedYaml:
        return False
    return True


def _split_key(content: str, line_number: int) -> tuple:
    """Split ``key: value`` at the first colon that is followed by space or EOL."""
    quoted = ""
    for position, char in enumerate(content):
        if quoted:
            if char == quoted:
                quoted = ""
            continue
        if char in _QUOTES:
            quoted = char
            continue
        if char != ":":
            continue
        following = content[position + 1 : position + 2]
        if following not in ("", " "):
            continue
        key = content[:position].strip()
        if not key:
            raise UnsupportedYaml(f"line {line_number + 1}: empty mapping key")
        if key[0] in _QUOTES or key[0] in "[{&*!|>":
            raise UnsupportedYaml(
                f"line {line_number + 1}: unsupported key form {key!r}"
            )
        return key, content[position + 1 :].strip()
    raise UnsupportedYaml(f"line {line_number + 1}: expected 'key: value'")


def _block_scalar(
    lines: list, index: int, indent: int, style: str, marker_line: int
) -> tuple:
    body: list = []
    cursor = index
    while cursor < len(lines):
        line = lines[cursor]
        if line.strip() and _indent_of(line) <= indent:
            break
        body.append(line)
        cursor += 1
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    widths = [_indent_of(line) for line in body if line.strip()]
    trim = min(widths) if widths else 0
    body = [line[trim:] if line.strip() else "" for line in body]

    if style.startswith("|"):
        return _chomp("\n".join(body), style), cursor
    folded: list = []
    for line in body:
        if not line:
            folded.append("\n")
        elif folded and not folded[-1].endswith("\n"):
            folded.append(" " + line)
        else:
            folded.append(line)
    return _chomp("".join(folded), style), cursor


def _chomp(text: str, style: str) -> str:
    """Apply YAML's chomping indicator: ``-`` strips, ``+`` keeps, none clips.

    Ignoring it would report a trailing newline for ``>-`` that YAML does not
    produce, and a parser that quietly disagrees with the file it read is worth
    less than no parser at all.
    """
    if style.endswith("-"):
        return text.rstrip("\n")
    if style.endswith("+"):
        return text + "\n"
    return text.rstrip("\n") + "\n" if text else ""


def _scalar(text: str, line_number: int) -> object:
    text = _strip_comment(text).strip()
    if text == "":
        return None
    if text.startswith("["):
        if not text.endswith("]"):
            raise UnsupportedYaml(
                f"line {line_number + 1}: flow sequence must close on its own line"
            )
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part, line_number) for part in inner.split(",")]
    if text.startswith("{"):
        raise UnsupportedYaml(f"line {line_number + 1}: flow mappings are unsupported")
    if text[0] in _QUOTES:
        quote = text[0]
        if len(text) < 2 or not text.endswith(quote):
            raise UnsupportedYaml(f"line {line_number + 1}: unterminated quoted scalar")
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    return text


def _strip_comment(text: str) -> str:
    """Cut a trailing ``# comment``, respecting quotes."""
    quoted = ""
    for position, char in enumerate(text):
        if quoted:
            if char == quoted:
                quoted = ""
            continue
        if char in _QUOTES:
            quoted = char
            continue
        if char == "#" and (position == 0 or text[position - 1] in " \t"):
            return text[:position]
    return text
