from collections import Counter
from pathlib import Path

import pytest

from dispute_casework.ecfr import Paragraph, parse_part

SNAPSHOT_XML = Path(__file__).parents[1] / "corpus/ecfr/2026-09-29/part-1005.xml"
SUPPLEMENT_HEADING = "Supplement I to Part 1005—Official Interpretations"


def section(number: str, *paragraphs: str) -> str:
    body = "".join(f"<P>{paragraph}</P>" for paragraph in paragraphs)
    return f'<DIV8 N="{number}"><HEAD>§ {number} Title.</HEAD>{body}</DIV8>'


def parse(sections: str = "", supplement: str = "") -> list[Paragraph]:
    return parse_part(
        f'<DIV5 N="1005">{sections}'
        f'<DIV9 N="Supplement I to Part 1005"><HEAD>{SUPPLEMENT_HEADING}</HEAD>'
        f"{supplement}</DIV9></DIV5>".encode()
    )


def ids(paragraphs: list[Paragraph]) -> list[str]:
    return [paragraph.paragraph_id for paragraph in paragraphs]


def test_adjacent_designators_take_the_deepest_level() -> None:
    paragraphs = parse(section("1005.2", "(a)(1) One.", "(2) Two.", "(b) Three."))
    assert ids(paragraphs) == ["1005.2(a)(1)", "1005.2(a)(2)", "1005.2(b)"]


def test_em_dash_chain_and_inherited_headings() -> None:
    paragraphs = parse(
        section(
            "1005.11",
            "(a) <I>Definition</I>—(1) <I>Covered.</I> The term means:",
            "(i) A first kind;",
            "(b) <I>Notice.</I> Text.",
        )
    )
    assert ids(paragraphs) == ["1005.11(a)(1)", "1005.11(a)(1)(i)", "1005.11(b)"]
    assert [paragraph.heading_path for paragraph in paragraphs] == [
        ("§ 1005.11 Title.",),
        ("§ 1005.11 Title.", "(a) Definition", "(1) Covered."),
        ("§ 1005.11 Title.",),
    ]
    assert paragraphs[0].text == "(a) Definition—(1) Covered. The term means:"


def test_heading_then_designator() -> None:
    paragraphs = parse(
        section(
            "1005.6",
            "(a) Text.",
            "(b) <I>Limits.</I> Text.",
            "(5) <I>Notice.</I> (i) Notice is given when sent.",
            "(ii) In person or in writing.",
        )
    )
    assert ids(paragraphs)[1:] == ["1005.6(b)", "1005.6(b)(5)(i)", "1005.6(b)(5)(ii)"]
    assert paragraphs[3].heading_path == (
        "§ 1005.6 Title.",
        "(b) Limits.",
        "(5) Notice.",
    )


def test_letter_i_after_h_and_roman_i_after_a_number() -> None:
    paragraphs = parse(
        section(
            "1005.2",
            "(a) Text.",
            "(b)(1) Text.",
            "(i) Roman one.",
            "(ii) Roman two.",
            *(f"({letter}) Text." for letter in "cdefgh"),
            "(i) Letter i.",
            "(j) Letter j.",
        )
    )
    assert ids(paragraphs)[2:4] == ["1005.2(b)(1)(i)", "1005.2(b)(1)(ii)"]
    assert ids(paragraphs)[-2:] == ["1005.2(i)", "1005.2(j)"]


@pytest.mark.parametrize(
    ("after_i", "expected"),
    [
        ("(ii) Second roman.", "1005.20(h)(2)(i)"),
        ("(j) Next letter.", "1005.20(i)"),
    ],
)
def test_i_after_h_with_numbered_children_is_settled_by_what_follows(
    after_i: str, expected: str
) -> None:
    letters = [f"({letter}) Text." for letter in "abcdefg"]
    paragraphs = parse(
        section(
            "1005.20",
            *letters,
            "(h) <I>Dates.</I> (1) One.",
            "(2) Two.",
            "(i) Ambiguous.",
            after_i,
        )
    )
    assert ids(paragraphs)[-2] == expected


def test_italic_designators_are_the_fifth_and_sixth_levels() -> None:
    paragraphs = parse(
        section(
            "1005.2",
            "(a)(1) Text.",
            "(i) Text.",
            "(A) Text.",
            "(<I>1</I>) Text.",
            "(<I>i</I>) Text.",
            "(<I>2</I>) Text.",
            "(2) Text.",
        )
    )
    assert ids(paragraphs)[3:] == [
        "1005.2(a)(1)(i)(A)(1)",
        "1005.2(a)(1)(i)(A)(1)(i)",
        "1005.2(a)(1)(i)(A)(2)",
        "1005.2(a)(2)",
    ]


def test_each_section_starts_its_own_outline() -> None:
    paragraphs = parse(
        section("1005.11", "(a) Text.", "(b) Text.")
        + section("1005.33", "(a) Text.", "(b) Text.")
    )
    assert ids(paragraphs) == [
        "1005.11(a)",
        "1005.11(b)",
        "1005.33(a)",
        "1005.33(b)",
    ]
    assert {paragraph.section for paragraph in paragraphs} == {"1005.11", "1005.33"}


def test_references_inside_the_text_are_not_designators() -> None:
    paragraphs = parse(
        section(
            "1005.11",
            "(a) See paragraphs (b)(1)(i) through (vi) and § 1005.6(a).",
            "(b) A request (other than one under paragraph (a)) is covered.",
        )
    )
    assert ids(paragraphs) == ["1005.11(a)", "1005.11(b)"]


def test_undesignated_opening_paragraph_takes_the_section_id() -> None:
    paragraphs = parse(section("1005.2", "For this part:", "(a) Text."))
    assert ids(paragraphs) == ["1005.2", "1005.2(a)"]


def test_undesignated_paragraph_after_a_designated_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="undesignated"):
        parse(section("1005.2", "(a) Text.", "Stray text."))


def test_designator_that_fits_nowhere_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not follow"):
        parse(section("1005.2", "(a) Text.", "(c) Text."))


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


def test_regulation_text_is_binding() -> None:
    (paragraph,) = parse(section("1005.13", "(a) Text."))
    assert (paragraph.source, paragraph.legal_status) == ("regulation", "binding")


def test_snapshot_yields_one_unique_id_per_paragraph() -> None:
    paragraphs = parse_part(SNAPSHOT_XML.read_bytes())
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
    assert {"1005.2", "1005.30", "1005.35", "1005.17(b)(3)-1"} <= by_id.keys()
