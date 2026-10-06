"""Paragraphs of one part of the eCFR, each with its ID.

Regulation text is read from the eCFR's rendered HTML, where every paragraph is
a <div id="p-1005.11(c)(2)(i)"> nested inside its parent paragraph. The ID is
that id without "p-".

Supplement I is read from the XML, which is flat: every comment is a <P> whose
number, "1." or "i.", is plain text at its start, and the outline is rebuilt
from the order of those numbers. Comments get IDs such as 1005.11(c)-3 and
1005.11(c)(4)-5.i. A comment that opens two levels at once, "1. Heading. i.
Heading.", takes the deeper one.
"""

import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

PARSER_VERSION = "1"
LEGAL_STATUS = {"regulation": "binding", "commentary": "official_interpretation"}

# Italic runs are wrapped in ⟨ ⟩ so that the pattern can pick up the italic
# heading that follows a comment number. A few comments carry their number
# inside the italic: "<I>1. Amount received.</I>".
COMMENT = re.compile(
    r"⟨?(?P<plain>\d+|[ivx]+|[A-Z])\.\s+(?:⟨?(?P<heading>[^⟨⟩]+)⟩)?\s*"
)
SECTION_HEADING = re.compile(r"Section (\d+\.\d+)")
ANCHOR_HEADING = re.compile(r"(?:Paragraph )?(\d+(?:\(\w+\))+)")
APPENDIX_HEADING = re.compile(r"Appendix ([A-Z])")
# A heading whose tag was lost in the source sits at the end of the preceding
# paragraph as "…transactions.2>17(b)(3) Same Account Terms, …".
LOST_HEADING_TAG = re.compile(r"\d>(?=\d+\()")
# Two comments printed as one paragraph: "…or confirmation.2. ⟨No disclosures…".
# A citation before an italic, "§ 1005.6. ⟨See also⟩", has a digit before the period.
FUSED_COMMENT = re.compile(r"(?<=[^\d\s]\.)(?=\d+\. ⟨)")


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


def parse_part(xml: bytes, html: bytes) -> list[Paragraph]:
    root = ET.fromstring(xml)
    part = root.attrib["N"]
    paragraphs = list(regulation_paragraphs(part, html))
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


def regulation_paragraphs(part: str, html: bytes) -> Iterator[Paragraph]:
    for section in BeautifulSoup(html, "html.parser").select("div.section"):
        number = str(section["id"])
        heading = html_text(section.select_one(":scope > h4"))
        # A paragraph directly inside the section has no id of its own.
        for paragraph in section.select(":scope > p:not(.citation)"):
            yield Paragraph(
                paragraph_id=number,
                part=part,
                section=number,
                source="regulation",
                heading_path=(heading,),
                text=html_text(paragraph),
            )
        yield from nested_paragraphs(part, number, section, (heading,))


def nested_paragraphs(
    part: str, section: str, parent: Tag, heading_path: tuple[str, ...]
) -> Iterator[Paragraph]:
    for node in parent.select(":scope > div[id]"):
        paragraph_id = str(node["id"]).removeprefix("p-")
        text = html_text(node.select_one(":scope > p"))
        if not text:
            raise ValueError(f"{paragraph_id} has no paragraph text")
        designator = html_text(node.select_one(":scope > p > .paragraph-hierarchy"))
        heading = html_text(node.select_one(":scope > p > .paragraph-heading"))
        label = f"{designator} {heading}".strip()
        # "(a) Definition of error —" only introduces the paragraphs below it.
        if text.removeprefix(label).strip(" —"):
            yield Paragraph(
                paragraph_id=paragraph_id,
                part=part,
                section=section,
                source="regulation",
                heading_path=heading_path,
                text=text,
            )
        yield from nested_paragraphs(
            part, section, node, (*heading_path, label) if heading else heading_path
        )


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
        # Where the source prints a run of comments a second time, not always
        # word for word, the number goes back; the second run and its
        # sub-paragraphs are dropped.
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
            comments, *lost_heading = LOST_HEADING_TAG.split(marked_text(element), 1)
            for comment in FUSED_COMMENT.split(comments):
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


def html_text(tag: Tag | None) -> str:
    return " ".join(tag.get_text().split()) if tag else ""
