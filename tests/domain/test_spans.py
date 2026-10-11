"""F2: the sentences and two-sentence windows the Fact-Checker compares a Claim with."""

from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.passages import (
    MAX_SENTENCE_CHARS,
    MAX_WINDOW_SENTENCES,
    Span,
    candidate_spans,
    sentences_of,
    window_width,
)

PARAGRAPH = ("BYD sold 4.27 million vehicles in 2025, up from 3.02 million in 2024. Plug-in hybrids "
             "made up about half of those sales. The company opened plants in Hungary and Brazil.")


def passage(text: str, index: int = 0) -> Passage:
    return Passage(observation_id="obs_1", index=index, text=text)


def test_sentences_split_at_stops_and_keep_decimals_and_figures_whole():
    assert sentences_of(PARAGRAPH) == [
        "BYD sold 4.27 million vehicles in 2025, up from 3.02 million in 2024.",
        "Plug-in hybrids made up about half of those sales.",
        "The company opened plants in Hungary and Brazil."]
    assert sentences_of('Sales rose! Did "they" fall? "No." Then 2025.') == [
        "Sales rose!", 'Did "they" fall?', '"No."', "Then 2025."]


def test_a_short_abbreviation_or_initials_do_not_end_a_sentence():
    assert sentences_of("BYD Co. Ltd. sold 4.27 million vehicles. Sales in the U.S. fell.") == [
        "BYD Co. Ltd. sold 4.27 million vehicles.", "Sales in the U.S. fell."]
    # An acronym is not an abbreviation, and a lower-case next word continues the sentence.
    assert sentences_of("Sold by BYD. Then approx. half were hybrids.") == [
        "Sold by BYD.", "Then approx. half were hybrids."]


def test_a_sentence_longer_than_the_cap_is_cut_into_bounded_pieces():
    text = " ".join(["word"] * 400)  # 1999 characters, no stop
    pieces = sentences_of(text)
    assert len(pieces) == 4 and all(len(p) <= MAX_SENTENCE_CHARS for p in pieces)
    assert " ".join(pieces) == text
    assert sentences_of("") == [] and sentences_of("   ") == []


def test_spans_are_each_sentence_then_its_window_with_the_next_and_name_their_passage():
    first, second = passage(PARAGRAPH), passage("One sentence only.", index=1)
    spans = candidate_spans([first, second])
    s = sentences_of(PARAGRAPH)
    assert spans == [
        Span(first, s[0]), Span(first, f"{s[0]} {s[1]}"),
        Span(first, s[1]), Span(first, f"{s[1]} {s[2]}"),
        Span(first, s[2]),
        Span(second, "One sentence only.")]


def test_windows_are_as_wide_as_the_claim_from_two_up_to_four_sentences():
    assert window_width("One sentence.") == 2
    assert window_width(PARAGRAPH) == 3
    assert window_width(" ".join(["A claim sentence."] * 9)) == MAX_WINDOW_SENTENCES == 4
    p = passage(PARAGRAPH)
    s = sentences_of(PARAGRAPH)
    assert [x.text for x in candidate_spans([p], width=3)] == [
        s[0], f"{s[0]} {s[1]}", PARAGRAPH,
        s[1], f"{s[1]} {s[2]}",
        s[2]]
    assert candidate_spans([p], width=99) == candidate_spans([p], width=MAX_WINDOW_SENTENCES)


def test_spans_are_deterministic_and_bounded():
    text = " ".join(f"Sentence number {n} ends here." for n in range(50))
    spans = candidate_spans([passage(text)])
    assert spans == candidate_spans([passage(text)])
    assert len(spans) == 2 * 50 - 1
    assert all(len(s.text) <= 2 * MAX_SENTENCE_CHARS + 1 for s in spans)
    widest = candidate_spans([passage(text)], width=MAX_WINDOW_SENTENCES)
    assert len(widest) == 4 * 50 - 6  # windows that would run past the end are not made
    assert all(len(s.text) <= 4 * (MAX_SENTENCE_CHARS + 1) for s in widest)
    assert candidate_spans([]) == []
