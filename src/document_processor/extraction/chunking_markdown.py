from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from src.document_processor.markdown_quality import (
    CAPTION_HEADING_RE,
    HEADING_RE,
    LIST_ITEM_RE,
    PAGE_MARKER_RE,
    TABLE_ROW_RE,
    is_layout_table,
    is_suspicious_heading,
    is_table_separator,
    table_cells,
)

from .layout import Layout

_WORD_RE = re.compile(r"[^\W\d_]+")
_LOST_LIGATURE = "�"
_DAMAGED_WORD_RE = re.compile(r"[^\W\d_]{1,40}(?:�[^\W\d_]{1,40}){1,3}")
_LIGATURES = ("ti", "tt", "fi", "fl", "ff", "ffi", "ffl", "ft", "th")
_MIN_REPAIRED_LENGTH = 4
_HYPHEN_SPACE_RE = re.compile(
    r"\b([^\W\d_]{2,})- (?!(?:and|or|to|nor|y|o|e|u|ni)\b)([a-záéíóúüñ][^\W\d_]*)"
)
_NUMBERING_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2})*)(\.?\s*\|\s*|\.\s+|\s+)(?=\S)")
_PAGE_NUMBER_RE = re.compile(r"<!-- page: (\d+) -->")
_AUTHOR_FOOTER_RE = re.compile(r"\bet al\.?,?\s*\d+\s*$", re.I)
_LETTER_SPACED_RE = re.compile(r"(?:\w ){3,}\w")
_SENTENCE_END_RE = re.compile(r"[.!?:;)\]\"”»]\s*$")
_INNER_BULLET = " • "
_MIN_BULLET_PARTS = 3
_MIN_LETTERS_IN_HEADING = 3
_MAX_PROMOTED_WORDS = 12
_MIN_NUMBERED_TO_PROMOTE = 2
_FONT_TOLERANCE = 0.25
_TOP_LEVEL = 2
_MIN_RUNNING_HEADER_PAGES = 2
_COUNTER_LABEL_RE = re.compile(
    r"(?:chapter|cap[ií]tulo|part|parte|section|secci[oó]n|case|caso|standard|est[aá]ndar|"
    r"statement|recommendation|recomendaci[oó]n|appendix|anexo|module|m[oó]dulo|question|pregunta)"
    r"\b",
    re.I,
)
_TITLE_CANDIDATES = 5
_MAX_LEVEL = 6


@dataclass
class _Heading:
    index: int
    text: str
    numbering: str = ""
    font_size: float | None = None
    level: int = 0


def _vocabulary(markdown: str) -> Counter[str]:
    return Counter(word.casefold() for word in _WORD_RE.findall(markdown))


def _with_case_of(ligature: str, word: str) -> str:
    letters = word.replace(_LOST_LIGATURE, "")
    return ligature.upper() if len(letters) > 1 and letters.isupper() else ligature


def _repair_lost_ligatures(markdown: str, vocabulary: Counter[str]) -> str:
    if _LOST_LIGATURE not in markdown:
        return markdown
    prefixes = {word[:end] for word in vocabulary for end in range(1, len(word) + 1)}
    repaired: dict[str, str] = {}

    def _repair(word: str) -> str:
        parts = word.split(_LOST_LIGATURE)
        partial = [parts[0]]
        for part in parts[1:]:
            partial = [
                candidate
                for prefix in partial
                for ligature in _LIGATURES
                if (candidate := prefix + _with_case_of(ligature, word) + part).casefold()
                in prefixes
            ]
        known = [c for c in partial if len(c) >= _MIN_REPAIRED_LENGTH and vocabulary[c.casefold()]]
        return max(known, key=lambda c: vocabulary[c.casefold()]) if known else word

    def _replace(match: re.Match[str]) -> str:
        word = match[0]
        if word not in repaired:
            repaired[word] = _repair(word)
        return repaired[word]

    return _DAMAGED_WORD_RE.sub(_replace, markdown)


def _hyphen_vocabulary(markdown: str) -> Counter[str]:
    return _vocabulary(_HYPHEN_SPACE_RE.sub(" ", markdown))


def _repair_hyphen_spaces(markdown: str, vocabulary: Counter[str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        first, second = match.groups()
        joined = f"{first}{second}"
        is_compound = vocabulary[first.casefold()] and vocabulary[second.casefold()]
        return joined if vocabulary[joined.casefold()] and not is_compound else f"{first}-{second}"

    return _HYPHEN_SPACE_RE.sub(_replace, markdown)


def _blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", markdown):
        pending: list[str] = []
        for line in block.strip("\n").splitlines():
            if HEADING_RE.match(line):
                blocks.extend(filter(None, ["\n".join(pending), line]))
                pending = []
            else:
                pending.append(line)
        if "\n".join(pending).strip():
            blocks.append("\n".join(pending))
    return blocks


def _is_prose_block(block: str) -> bool:
    first = block.splitlines()[0]
    return not (
        HEADING_RE.match(first)
        or TABLE_ROW_RE.match(first)
        or LIST_ITEM_RE.match(first)
        or PAGE_MARKER_RE.match(first.strip())
        or first.startswith("**")
    )


def _continues(previous: str, following: str) -> bool:
    return (
        _is_prose_block(previous)
        and _is_prose_block(following)
        and not _SENTENCE_END_RE.search(previous)
        and following[:1].islower()
    )


def _relocate_page_markers(blocks: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        is_split_paragraph = (
            PAGE_MARKER_RE.match(block.strip())
            and result
            and index + 1 < len(blocks)
            and _continues(result[-1], blocks[index + 1])
        )
        if is_split_paragraph:
            result[-1] = f"{result[-1]} {blocks[index + 1]}"
            result.append(block)
            index += 2
            continue
        result.append(block)
        index += 1
    return result


def _is_table_block(block: str) -> bool:
    return bool(TABLE_ROW_RE.match(block.splitlines()[0]))


def _linearize_layout_table(block: str) -> str:
    rows = [table_cells(line) for line in block.splitlines() if not is_table_separator(line)]
    if not rows:
        return block
    header = rows[0]
    header_is_mostly_empty = sum(1 for cell in header if cell) * 2 < len(header)
    if not (header_is_mostly_empty and is_layout_table(rows)):
        return block
    lines = ["; ".join(cell for cell in row if cell) for row in rows]
    return "\n".join(f"- {line}" for line in lines if line)


def _split_inline_bullets(block: str) -> str:
    lines: list[str] = []
    for line in block.splitlines():
        opens_with_bullet = line.lstrip().startswith("•")
        body = line.lstrip("-*• ").strip() if opens_with_bullet else line
        parts = [part.strip() for part in body.split(_INNER_BULLET)]
        if len(parts) < _MIN_BULLET_PARTS - opens_with_bullet or TABLE_ROW_RE.match(line):
            lines.append(line)
            continue
        lead, items = ("", parts) if opens_with_bullet else (parts[0], parts[1:])
        if lead:
            lines.extend([lead, ""])
        lines.extend(f"- {item}" for item in items if item)
        lines.append("")
    return "\n".join(lines).strip("\n")


def _without_digits(text: str) -> str:
    return re.sub(r"\d+", "", text).strip().casefold()


def _page_after_marker(blocks: list[str]) -> list[int | None]:
    pages: list[int | None] = []
    for previous in [""] + blocks[:-1] if blocks else []:
        marker = _PAGE_NUMBER_RE.fullmatch(previous.strip())
        pages.append(int(marker[1]) if marker else None)
    return pages


def _page_offsets(text: str, page: int) -> set[int]:
    return {int(number) - page for number in re.findall(r"\d+", text)}


def _running_header_offsets(blocks: list[str], pages: list[int | None]) -> dict[str, set[int]]:
    total: Counter[str] = Counter()
    offsets: dict[str, list[set[int]]] = defaultdict(list)
    for block, page in zip(blocks, pages, strict=True):
        if match := HEADING_RE.match(block):
            key = _without_digits(match[2])
            total[key] += 1
            is_counter_label = bool(_COUNTER_LABEL_RE.match(key))
            if page is not None and not is_counter_label and not CAPTION_HEADING_RE.match(match[2]):
                offsets[key].append(_page_offsets(match[2], page))
    return {
        key: set.intersection(*found)
        for key, found in offsets.items()
        if key
        and len(found) >= _MIN_RUNNING_HEADER_PAGES
        and len(found) * 2 >= total[key]
        and set.intersection(*found)
    }


def _is_page_furniture(text: str, offsets: dict[str, set[int]], page: int | None) -> bool:
    if page is None:
        return False
    tracks_page = bool(offsets.get(_without_digits(text), set()) & _page_offsets(text, page))
    return tracks_page or bool(_AUTHOR_FOOTER_RE.search(text))


def _drop_page_furniture(blocks: list[str]) -> list[str]:
    pages = _page_after_marker(blocks)
    offsets = _running_header_offsets(blocks, pages)
    return [
        block
        for block, page in zip(blocks, pages, strict=True)
        if not ((match := HEADING_RE.match(block)) and _is_page_furniture(match[2], offsets, page))
    ]


def _is_noise_heading(text: str) -> bool:
    letters = sum(1 for char in text if char.isalpha())
    return (
        letters < _MIN_LETTERS_IN_HEADING
        or is_suspicious_heading(text)
        or bool(_LETTER_SPACED_RE.fullmatch(text))
    )


def _layout_key(text: str) -> str:
    return re.sub(r"\W+", "", text.casefold())


def _font_sizes(layout: Layout | None, repair: dict[str, Counter[str]]) -> dict[str, list[float]]:
    sizes: dict[str, list[float]] = defaultdict(list)
    for heading in layout.headings if layout else ():
        if heading.font_size is None:
            continue
        text = _repair_lost_ligatures(heading.text, repair["ligatures"])
        text = _repair_hyphen_spaces(text, repair["hyphens"])
        sizes[_layout_key(text)].append(heading.font_size)
    return sizes


def _promotable_numbering(blocks: list[str]) -> str | None:
    separators = Counter(
        numbered[2].strip()
        for block in blocks
        if (heading := HEADING_RE.match(block)) and (numbered := _NUMBERING_RE.match(heading[2]))
    )
    if not separators:
        return None
    separator, times = separators.most_common(1)[0]
    return separator if times >= _MIN_NUMBERED_TO_PROMOTE and separator else None


def _promotable_depth(block: str, separator: str | None) -> int | None:
    numbered = _NUMBERING_RE.match(block)
    promotable = bool(
        separator
        and numbered
        and "\n" not in block
        and numbered[2].strip() == separator
        and len(block.split()) <= _MAX_PROMOTED_WORDS
        and not _SENTENCE_END_RE.search(block)
    )
    return numbered[1].count(".") if promotable and numbered else None


def _promotions(blocks: list[str]) -> list[bool]:
    separator = _promotable_numbering(blocks)
    content = [i for i, block in enumerate(blocks) if not PAGE_MARKER_RE.match(block.strip())]
    depths = {i: _promotable_depth(blocks[i], separator) for i in content}
    neighbours = {
        i: {depths[j] for j in content[max(0, k - 1) : k + 2] if j != i}
        for k, i in enumerate(content)
    }
    return [
        (depth := depths.get(i)) is not None and depth not in neighbours[i]
        for i in range(len(blocks))
    ]


def _collect_headings(
    blocks: list[str], layout: Layout | None, repair: dict[str, Counter[str]]
) -> tuple[list[str], list[_Heading]]:
    sizes = _font_sizes(layout, repair)
    body = layout.body_font_size if layout else None
    kept: list[str] = []
    headings: list[_Heading] = []
    for block, promote in zip(blocks, _promotions(blocks), strict=True):
        match = HEADING_RE.match(block)
        text = match[2] if match else block
        if not match and not promote:
            kept.append(block)
            continue
        if match and CAPTION_HEADING_RE.match(text):
            kept.append(f"**{text}**")
            continue
        font = sizes[_layout_key(text)].pop(0) if sizes.get(_layout_key(text)) else None
        numbered = _NUMBERING_RE.match(text)
        smaller_than_body = font is not None and body is not None and font < body - _FONT_TOLERANCE
        if match and (_is_noise_heading(text) or (smaller_than_body and not numbered)):
            kept.append(text)
            continue
        headings.append(
            _Heading(
                index=len(kept),
                text=text,
                numbering=numbered[1] if numbered else "",
                font_size=font,
            )
        )
        kept.append(text)
    return kept, headings


def _title_index(headings: list[_Heading]) -> int | None:
    candidates = [
        (position, heading)
        for position, heading in enumerate(headings[:_TITLE_CANDIDATES])
        if not heading.numbering
    ]
    if not candidates:
        return None
    sized = [heading.font_size for _, heading in candidates if heading.font_size is not None]
    if not sized:
        return candidates[0][0]
    return next(position for position, heading in candidates if heading.font_size == max(sized))


def _levels_by_font(headings: list[_Heading], title: int | None) -> dict[float, int]:
    anchors: dict[float, Counter[int]] = defaultdict(Counter)
    for heading in headings:
        if heading.numbering and heading.font_size is not None:
            anchors[heading.font_size][_TOP_LEVEL + heading.numbering.count(".")] += 1
    sizes = sorted(
        {h.font_size for i, h in enumerate(headings) if h.font_size is not None and i != title},
        reverse=True,
    )
    levels = {size: min(_TOP_LEVEL + rank, _MAX_LEVEL) for rank, size in enumerate(sizes)}
    levels.update({size: votes.most_common(1)[0][0] for size, votes in anchors.items()})
    return levels


def _assign_levels(headings: list[_Heading]) -> None:
    title = _title_index(headings)
    by_font = _levels_by_font(headings, title)
    previous = 1
    for position, heading in enumerate(headings):
        if position == title:
            heading.level = 1
            previous = 1
            continue
        if heading.numbering:
            wanted = _TOP_LEVEL + heading.numbering.count(".")
        elif heading.font_size is not None:
            wanted = by_font[heading.font_size]
        else:
            wanted = max(previous, _TOP_LEVEL)
        heading.level = max(_TOP_LEVEL, min(wanted, previous + 1, _MAX_LEVEL))
        previous = heading.level


def _normalize_headings(
    blocks: list[str], layout: Layout | None, repair: dict[str, Counter[str]]
) -> list[str]:
    kept, headings = _collect_headings(blocks, layout, repair)
    _assign_levels(headings)
    for heading in headings:
        kept[heading.index] = f"{'#' * heading.level} {heading.text}"
    return kept


def normalize_for_chunking(markdown: str, layout: Layout | None) -> str:
    markdown = "\n\n".join(_relocate_page_markers(_drop_page_furniture(_blocks(markdown))))
    repair = {"ligatures": _vocabulary(markdown), "hyphens": _hyphen_vocabulary(markdown)}
    markdown = _repair_lost_ligatures(markdown, repair["ligatures"])
    markdown = _repair_hyphen_spaces(markdown, repair["hyphens"])
    blocks = [
        _linearize_layout_table(block) if _is_table_block(block) else block
        for block in _blocks(markdown)
    ]
    blocks = [
        block if HEADING_RE.match(block) else _split_inline_bullets(block) for block in blocks
    ]
    blocks = _normalize_headings(blocks, layout, repair)
    return "\n\n".join(blocks) + "\n"