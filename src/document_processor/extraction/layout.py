from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .markdown_cleaner import fix_ligatures
from .redaction import redact_personal_data

_CHILD_KEYS = ("kids", "rows", "cells", "list items")


@dataclass(frozen=True)
class LayoutHeading:
    text: str
    page: int
    font_size: float | None
    level: int


@dataclass(frozen=True)
class Layout:
    pages: int
    body_font_size: float | None
    headings: tuple[LayoutHeading, ...]


_Element = tuple[dict[str, Any], bool]


def _elements(nodes: list[Any], inside_table: bool = False) -> Iterator[_Element]:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node, inside_table
        nested = inside_table or node.get("type") == "table"
        for key in _CHILD_KEYS:
            yield from _elements(node.get(key) or [], nested)


def _text(node: dict[str, Any]) -> str:
    return redact_personal_data(fix_ligatures(str(node.get("content") or ""))).strip()


def _optional_number(node: dict[str, Any], key: str) -> float | None:
    try:
        return float(node[key])
    except (KeyError, TypeError, ValueError):
        return None


def _number(node: dict[str, Any], key: str) -> float:
    return _optional_number(node, key) or 0.0


def summarize_layout(document: dict[str, Any]) -> Layout:
    body_chars: Counter[float] = Counter()
    headings: list[LayoutHeading] = []
    for node, inside_table in _elements(document.get("kids") or []):
        kind = node.get("type")
        if kind == "paragraph" and _number(node, "font size"):
            body_chars[_number(node, "font size")] += len(str(node.get("content") or ""))
        elif kind == "heading" and not inside_table:
            headings.append(
                LayoutHeading(
                    text=_text(node),
                    page=int(_number(node, "page number")),
                    font_size=_optional_number(node, "font size"),
                    level=int(_number(node, "heading level")),
                )
            )
    return Layout(
        pages=int(_number(document, "number of pages")),
        body_font_size=body_chars.most_common(1)[0][0] if body_chars else None,
        headings=tuple(headings),
    )