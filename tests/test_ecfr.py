from collections import Counter
from pathlib import Path

import pytest

from dispute_casework.ecfr import Paragraph, parse_part

SUPPLEMENT_HEADING = "Supplement I to Part 1005—Official Interpretations"


def mark(designator: str) -> str:
    return (
        '<span class="paragraph-hierarchy"><span class="paren">(</span>'
        f'{designator}<span class="paren">)</span></span>'
    )


def heading(text: str) -> str:
    return f'<em class="paragraph-heading">{text}</em>'


def node(node_id: str, paragraph: str, *children: str) -> str:
    return f'<div id="{node_id}"><p>{paragraph}</p>{"".join(children)}</div>'


def section(number: str, *content: str) -> str:
    return (
        f'<div class="section" id="{number}"><h4>§ {number} Title.</h4>'
        f"{''.join(content)}</div>"
    )


def parse(sections: str = "", supplement: str = "") -> list[Paragraph]:
    return parse_part(
        '<DIV5 N="1005"><DIV9 N="Supplement I to Part 1005">'
        f"<HEAD>{SUPPLEMENT_HEADING}</HEAD>{supplement}</DIV9></DIV5>".encode(),
        f'<div class="part" id="part-1005">{sections}</div>'.encode(),
    )


def ids(paragraphs: list[Paragraph]) -> list[str]:
    return [paragraph.paragraph_id for paragraph in paragraphs]


def test_regulation_id_is_the_node_id_without_its_prefix() -> None:
    paragraphs = parse(
        section(
            "1005.11",
            node(
                "p-1005.11(a)",
                f"{mark('a')} First.",
                node("p-1005.11(a)(1)", f"{mark('1')} Second."),
            ),
            node("p-as-the-file-has-it", f"{mark('b')} Third."),
        )
    )
    assert ids(paragraphs) == ["1005.11(a)", "1005.11(a)(1)", "as-the-file-has-it"]
    assert {paragraph.section for paragraph in paragraphs} == {"1005.11"}


def test_text_of_a_node_leaves_out_the_paragraphs_below_it() -> None:
    paragraphs = parse(
        section(
            "1005.6",
            node(
                "p-1005.6(a)",
                f"{mark('a')}  The consumer is liable\n if:",
                node("p-1005.6(a)(1)", f"{mark('1')} A device was used."),
            ),
        )
    )
    assert [paragraph.text for paragraph in paragraphs] == [
        "(a) The consumer is liable if:",
        "(1) A device was used.",
    ]


def test_node_with_only_designator_and_heading_gives_its_heading_to_its_children() -> (
    None
):
    paragraphs = parse(
        section(
            "1005.11",
            node(
                "p-1005.11(a)",
                f"{mark('a')} {heading('Definition of error')} —",
                node(
                    "p-1005.11(a)(1)",
                    f"{mark('1')} {heading('Covered.')}  The term means:",
                    node("p-1005.11(a)(1)(i)", f"{mark('i')} A first kind;"),
                ),
            ),
            node("p-1005.11(b)", f"{mark('b')} {heading('Notice.')} Text."),
        )
    )
    assert ids(paragraphs) == ["1005.11(a)(1)", "1005.11(a)(1)(i)", "1005.11(b)"]
    assert [paragraph.heading_path for paragraph in paragraphs] == [
        ("§ 1005.11 Title.", "(a) Definition of error"),
        ("§ 1005.11 Title.", "(a) Definition of error", "(1) Covered."),
        ("§ 1005.11 Title.",),
    ]
    assert paragraphs[0].text == "(1) Covered. The term means:"


def test_node_with_only_a_designator_adds_nothing_to_the_heading_path() -> None:
    paragraphs = parse(
        section(
            "1005.2",
            node(
                "p-1005.2(a)",
                mark("a"),
                node("p-1005.2(a)(1)", f"{mark('1')} “Access device” means a card."),
            ),
        )
    )
    assert ids(paragraphs) == ["1005.2(a)(1)"]
    assert paragraphs[0].heading_path == ("§ 1005.2 Title.",)
    assert paragraphs[0].text == "(1) “Access device” means a card."


def test_paragraph_directly_inside_a_section_takes_the_section_id() -> None:
    paragraphs = parse(
        section(
            "1005.2",
            "<p>For this part:</p>",
            node("p-1005.2(a)", f"{mark('a')} Text."),
            '<p class="citation">[76 FR 81023, Dec. 27, 2011]</p>',
        )
    )
    assert ids(paragraphs) == ["1005.2", "1005.2(a)"]
    assert paragraphs[0].text == "For this part:"


def test_node_without_paragraph_text_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"1005.2\(a\) has no paragraph text"):
        parse(section("1005.2", '<div id="p-1005.2(a)"></div>'))


def test_id_seen_twice_is_rejected() -> None:
    with pytest.raises(ValueError, match="more than once"):
        parse(
            section(
                "1005.2",
                node("p-1005.2(a)", f"{mark('a')} Text."),
                node("p-1005.2(a)", f"{mark('a')} Other text."),
            )
        )


def test_regulation_text_is_binding() -> None:
    (paragraph,) = parse(section("1005.13", node("p-1005.13(a)", "(a) Text.")))
    assert (paragraph.source, paragraph.legal_status) == ("regulation", "binding")


def test_supplement_anchors_and_comment_levels() -> None:
    paragraphs = parse(
        supplement=(
            "<HD1>Section 1005.11 Procedures</HD1>"
            "<HD2>11(c) Time Limits</HD2>"
            "<P>1. <I>Notice.</I> Text.</P>"
            "<P>2. <I>Examples.</I> Text:</P>"
            "<P>i. First.</P>"
            "<P>A. Detail.</P>"
            "<P>ii. Second.</P>"
            "<HD2>Paragraph 11(c)(2)(i)</HD2>"
            "<P>1. <I>Compliance.</I> Text.</P>"
        )
    )
    assert ids(paragraphs) == [
        "1005.11(c)-1",
        "1005.11(c)-2",
        "1005.11(c)-2.i",
        "1005.11(c)-2.i.A",
        "1005.11(c)-2.ii",
        "1005.11(c)(2)(i)-1",
    ]
    assert paragraphs[3].heading_path == (
        SUPPLEMENT_HEADING,
        "Section 1005.11 Procedures",
        "11(c) Time Limits",
        "2. Examples.",
    )
    assert {paragraph.section for paragraph in paragraphs} == {"1005.11"}
    assert {paragraph.source for paragraph in paragraphs} == {"commentary"}
    assert {paragraph.legal_status for paragraph in paragraphs} == {
        "official_interpretation"
    }


def test_supplement_section_headings_at_any_heading_level() -> None:
    paragraphs = parse(
        supplement=(
            "<HD1>Section 1005.30—Definitions</HD1>"
            "<P>1. Directly under the section.</P>"
            "<HD3>Section 1005.18—Prepaid Accounts</HD3>"
            "<HD3>18(a) Coverage</HD3>"
            "<P>1. Text.</P>"
            "<HD2>Section 1005.19 Posting</HD2>"
            "<HD2>19(b)(1) Submission</HD2>"
            "<P>1. Text.</P>"
            "<HD1>Appendix A—Model Forms</HD1>"
            "<P>1. Text.</P>"
        )
    )
    assert ids(paragraphs) == [
        "1005.30-1",
        "1005.18(a)-1",
        "1005.19(b)(1)-1",
        "1005.A-1",
    ]


def test_comment_opening_two_levels_and_number_inside_the_italic() -> None:
    paragraphs = parse(
        supplement=(
            "<HD1>Section 1005.17 Overdrafts</HD1>"
            "<HD2>17(b) Opt-In</HD2>"
            "<P>1. <I>Scope.</I> i. <I>Institutions.</I> Text.</P>"
            "<P>ii. <I>Coding.</I> Text.</P>"
            "<P><I>2. Amount received.</I> Text.</P>"
        )
    )
    assert ids(paragraphs) == [
        "1005.17(b)-1.i",
        "1005.17(b)-1.ii",
        "1005.17(b)-2",
    ]
    assert paragraphs[1].heading_path[-1] == "1. Scope."
    assert paragraphs[2].text == "2. Amount received. Text."


def test_heading_that_lost_its_tag_is_recovered() -> None:
    paragraphs = parse(
        supplement=(
            "<HD1>Section 1005.17 Overdrafts</HD1>"
            "<HD2>17(b)(2) Conditioning</HD2>"
            "<P>1. Text.2&gt;17(b)(3) Same Terms</P>"
            "<P>1. Other text.</P>"
        )
    )
    assert ids(paragraphs) == ["1005.17(b)(2)-1", "1005.17(b)(3)-1"]
    assert paragraphs[0].text == "1. Text."
    assert paragraphs[1].heading_path[-1] == "17(b)(3) Same Terms"


def test_comments_printed_twice_are_kept_once() -> None:
    paragraphs = parse(
        supplement=(
            "<HD1>Section 1005.32 Estimates</HD1>"
            "<HD2>32(b)(1) Exceptions</HD2>"
            "<P>1. Text.</P>"
            "<P>2. Text.</P>"
            "<P>i. Text.</P>"
            "<P>3. Text.</P>"
            "<P>2. Text.</P>"
            "<P>i. Text.</P>"
            "<P>3. Text.</P>"
            "<P>4. Text.</P>"
        )
    )
    assert ids(paragraphs) == [
        "1005.32(b)(1)-1",
        "1005.32(b)(1)-2",
        "1005.32(b)(1)-2.i",
        "1005.32(b)(1)-3",
        "1005.32(b)(1)-4",
    ]


def test_supplement_heading_of_an_unknown_form_is_rejected() -> None:
    with pytest.raises(ValueError, match="unrecognised"):
        parse(supplement="<HD1>General Notes</HD1><P>1. Text.</P>")


def test_two_comments_printed_as_one_paragraph_are_split() -> None:
    paragraphs = parse(
        supplement=(
            "<HD1>Section 1005.20 Gift Cards</HD1>"
            "<HD2>20(c)(4) Disclosures</HD2>"
            "<P>1. <I>Non-physical cards.</I> On the code or confirmation.2. "
            "<I>No disclosures.</I> Not needed. <I>See also</I> comment 20(c)(2)-2.</P>"
        )
    )
    assert ids(paragraphs) == ["1005.20(c)(4)-1", "1005.20(c)(4)-2"]
    assert [paragraph.text for paragraph in paragraphs] == [
        "1. Non-physical cards. On the code or confirmation.",
        "2. No disclosures. Not needed. See also comment 20(c)(2)-2.",
    ]


def test_citation_followed_by_see_also_is_not_split() -> None:
    (paragraph,) = parse(
        supplement=(
            "<HD1>Section 1005.7 Initial Disclosures</HD1>"
            "<HD2>7(b)(1) Liability</HD2>"
            "<P>4. <I>Change of address.</I> Liability under § 1005.6. "
            "<I>See also</I> § 1005.6(a).</P>"
        )
    )
    assert paragraph.paragraph_id == "1005.7(b)(1)-4"
    assert paragraph.text.endswith("under § 1005.6. See also § 1005.6(a).")


def test_snapshot_yields_one_unique_id_per_paragraph(snapshot_dir: Path) -> None:
    paragraphs = parse_part(
        (snapshot_dir / "part-1005.xml").read_bytes(),
        (snapshot_dir / "part-1005.html").read_bytes(),
    )
    assert Counter(paragraph.source for paragraph in paragraphs) == {
        "regulation": 741,
        "commentary": 1001,
    }
    assert len(set(ids(paragraphs))) == 1742
    assert len({paragraph.section for paragraph in paragraphs}) == 28
    by_id = {paragraph.paragraph_id: paragraph for paragraph in paragraphs}
    assert by_id["1005.11(c)(2)(i)(A)"].heading_path == (
        "§ 1005.11 Procedures for resolving errors.",
        "(c) Time limits and extent of investigation",
        "(2) Forty-five day period.",
    )
    assert by_id["1005.2(i)"].text.startswith("(i) “Financial institution”")
    assert by_id["1005.2(b)(3)(i)(D)(1)"].text.startswith("(1) That is issued")
    assert by_id["1005.11(c)(4)-5.i"].text.startswith("i. The ACH transaction")
    assert by_id["1005.20(c)(4)-2"].text.startswith("2. No disclosures on")
    assert {"1005.2", "1005.30", "1005.35", "1005.17(b)(3)-1"} <= by_id.keys()


def test_every_regulation_id_is_an_id_of_the_ecfr_file_verbatim(
    snapshot_dir: Path,
) -> None:
    html = (snapshot_dir / "part-1005.html").read_text()
    regulation = [
        paragraph
        for paragraph in parse_part(
            (snapshot_dir / "part-1005.xml").read_bytes(), html.encode()
        )
        if paragraph.source == "regulation"
    ]
    assert len(regulation) == 741
    for paragraph in regulation:
        assert (
            f'<div id="p-{paragraph.paragraph_id}">' in html
            or f'<div class="section" id="{paragraph.paragraph_id}">' in html
        )
