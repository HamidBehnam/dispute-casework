"""Paragraph IDs for one part of the eCFR XML.

The XML is flat: every paragraph is a <P> whose designators, "(c)" or "1.",
are plain text at its start. The outline is rebuilt from the order of those
designators. Regulation paragraphs get IDs such as 1005.11(c)(2)(i);
Supplement I comments get IDs such as 1005.11(c)-3 and 1005.11(c)(4)-5.i.
A paragraph that opens several levels at once, "(a) Heading—(1) Heading.",
takes the deepest one.
"""

import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

PARSER_VERSION = "0"
LEGAL_STATUS = {"regulation": "binding", "commentary": "official_interpretation"}

ROMAN = (
    "i ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx".split()
)

# Italic runs are wrapped in ⟨ ⟩ so that one pattern can tell "(1)" from "(<I>1</I>)"
# and can pick up the italic heading that follows a designator.
DESIGNATOR = re.compile(
    r"\((?:⟨(?P<italic>\d+|[ivx]+)⟩|(?P<plain>\d+|[a-z]+|[A-Z]+))\)"
    r"\s*(?:⟨(?P<heading>[^⟩]+)⟩)?[\s—]*"
)
# A few comments carry their number inside the italic: "<I>1. Amount received.</I>".
COMMENT = re.compile(
    r"⟨?(?P<plain>\d+|[ivx]+|[A-Z])\.\s+(?:⟨?(?P<heading>[^⟨⟩]+)⟩)?\s*"
)
SECTION_HEADING = re.compile(r"Section (\d+\.\d+)")
ANCHOR_HEADING = re.compile(r"(?:Paragraph )?(\d+(?:\(\w+\))+)")
APPENDIX_HEADING = re.compile(r"Appendix ([A-Z])")
# A heading whose tag was lost in the source sits at the end of the preceding
# paragraph as "…transactions.2>17(b)(3) Same Account Terms, …".
LOST_HEADING_TAG = re.compile(r"\d>(?=\d+\()")


@dataclass(frozen=True)
class Paragraph:
    paragraph_id: str
    part: str
    section: str
    source: str
    heading_path: tuple[str, ...]
    text: str

    @property
    def legal_status(self) -> str:
        return LEGAL_STATUS[self.source]


@dataclass
class Outline:
    """Designators and headings of the paragraph being read and of its ancestors."""

    path: list[str] = field(default_factory=list)
    headings: list[str | None] = field(default_factory=list)

    def enter(self, depth: int, designator: str, heading: str | None) -> list[str]:
        """Moves to a paragraph at this depth and returns its ancestors' headings."""
        if depth > len(self.path) + 1:
            raise ValueError(f"{designator} skips a level after {self.path}")
        inherited = [heading for heading in self.headings[: depth - 1] if heading]
        self.path[depth - 1 :] = [designator]
        self.headings[depth - 1 :] = [heading]
        return inherited


def parse_part(xml: bytes) -> list[Paragraph]:
    root = ET.fromstring(xml)
    part = root.attrib["N"]
    paragraphs = [
        paragraph
        for section in root.iter("DIV8")
        for paragraph in section_paragraphs(part, section)
    ]
    for appendix in root.iter("DIV9"):
        if appendix.attrib["N"].startswith("Supplement I"):
            paragraphs.extend(supplement_paragraphs(part, appendix))
    repeated = [
        paragraph_id
        for paragraph_id, count in Counter(p.paragraph_id for p in paragraphs).items()
        if count > 1
    ]
    if repeated:
        raise ValueError(f"paragraph IDs assigned more than once: {repeated}")
    return paragraphs


def section_paragraphs(part: str, section: ET.Element) -> Iterator[Paragraph]:
    number = section.attrib["N"]
    section_heading = plain_text(section.find("HEAD"))
    texts = [marked_text(element) for element in section.iter("P")]
    leading = [leading_matches(DESIGNATOR, text) for text in texts]
    later = [match for matches in leading for match in matches]
    outline = Outline()
    for text, matches in zip(texts, leading, strict=True):
        if not matches and outline.path:
            raise ValueError(f"undesignated paragraph inside § {number}")
        inherited: list[str] = []
        for match in matches:
            later = later[1:]
            designator = match["italic"] or match["plain"]
            above = outline.enter(
                regulation_depth(match, outline.path, later),
                designator,
                f"({designator}) {match['heading']}" if match["heading"] else None,
            )
            if match is matches[0]:
                inherited = above
        yield Paragraph(
            paragraph_id=number + "".join(f"({d})" for d in outline.path),
            part=part,
            section=number,
            source="regulation",
            heading_path=(section_heading, *inherited),
            text=unmarked(text),
        )


def regulation_depth(
    match: re.Match[str], path: list[str], later: list[re.Match[str]]
) -> int:
    """(a) is depth 1, then (1), (i), (A), and the italic (1) and (i) at 5 and 6.

    A lowercase designator such as (i) after (h)(2) can be the next letter or
    the first roman numeral. The next lowercase designator in the section
    settles it: a roman (i) is followed by (ii).
    """
    if match["italic"]:
        return 5 if match["italic"].isdigit() else 6
    designator = match["plain"]
    if designator.isdigit():
        return 2
    if designator.isupper():
        return 4
    is_letter = designator == (chr(ord(path[0]) + 1) if path else "a")
    is_roman = len(path) >= 2 and designator == next_roman(path[2:3])
    if is_letter and is_roman:
        next_lowercase = next(
            (m["plain"] for m in later if m["plain"] and m["plain"].islower()), None
        )
        is_letter = next_lowercase != next_roman([designator])
    elif not is_letter and not is_roman:
        raise ValueError(f"({designator}) does not follow {path}")
    return 1 if is_letter else 3


def next_roman(current: list[str]) -> str:
    return ROMAN[ROMAN.index(current[0]) + 1] if current else ROMAN[0]


def supplement_paragraphs(part: str, supplement: ET.Element) -> Iterator[Paragraph]:
    supplement_heading = plain_text(supplement.find("HEAD"))
    section = base = section_heading = anchor_heading = ""
    outline = Outline()
    repeating = False
    for is_heading, text in supplement_items(supplement):
        if is_heading:
            outline = Outline()
            repeating = False
            if found := SECTION_HEADING.match(text):
                section = base = found[1]
                section_heading, anchor_heading = text, ""
            elif found := APPENDIX_HEADING.match(text):
                section = base = f"{part}.{found[1]}"
                section_heading, anchor_heading = text, ""
            elif found := ANCHOR_HEADING.match(text):
                base = f"{part}.{found[1]}"
                anchor_heading = text
            else:
                raise ValueError(f"unrecognised Supplement I heading: {text}")
            continue
        matches = leading_matches(COMMENT, text)
        if not matches:
            raise ValueError(f"comment without a designator under {base}")
        number = matches[0]["plain"]
        # Where the source prints a run of comments twice, the number goes
        # back; the repeats and their sub-paragraphs are dropped.
        if number.isdigit():
            repeating = bool(outline.path) and int(number) <= int(outline.path[0])
        if repeating:
            continue
        inherited: list[str] = []
        for match in matches:
            designator = match["plain"]
            above = outline.enter(
                comment_depth(designator),
                designator,
                f"{designator}. {match['heading']}" if match["heading"] else None,
            )
            if match is matches[0]:
                inherited = above
        context = (supplement_heading, section_heading, anchor_heading, *inherited)
        yield Paragraph(
            paragraph_id=f"{base}-{'.'.join(outline.path)}",
            part=part,
            section=section,
            source="commentary",
            heading_path=tuple(heading for heading in context if heading),
            text=unmarked(text),
        )


def supplement_items(supplement: ET.Element) -> Iterator[tuple[bool, str]]:
    """Headings (True) and comment paragraphs (False) in document order."""
    for element in supplement:
        if element.tag in ("HD1", "HD2", "HD3"):
            yield True, plain_text(element)
        elif element.tag == "P":
            comment, *lost_heading = LOST_HEADING_TAG.split(marked_text(element), 1)
            yield False, comment
            if lost_heading:
                yield True, lost_heading[0]


def comment_depth(designator: str) -> int:
    """Comment 1. is depth 1, then i., then A."""
    if designator.isdigit():
        return 1
    return 2 if designator.islower() else 3


def leading_matches(pattern: re.Pattern[str], marked: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    while match := pattern.match(marked, matches[-1].end() if matches else 0):
        matches.append(match)
    return matches


def marked_text(element: ET.Element) -> str:
    parts = [element.text or ""]
    for child in element:
        parts += ["⟨", "".join(child.itertext()), "⟩", child.tail or ""]
    return " ".join("".join(parts).split())


def unmarked(marked: str) -> str:
    return marked.replace("⟨", "").replace("⟩", "")


def plain_text(element: ET.Element | None) -> str:
    if element is None:
        raise ValueError("heading element missing")
    return " ".join("".join(element.itertext()).split())
