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


def test_a_sentence_ends_only_before_a_capital_and_no_abbreviation_list_is_kept():
    # A stop before a lower-case word, a digit or a symbol does not end the sentence.
    assert sentences_of("Sales rose approx. 40% vs. 3.02 million, est. $5 billion. Then fell.") == [
        "Sales rose approx. 40% vs. 3.02 million, est. $5 billion.", "Then fell."]
    assert sentences_of("Sales in the U.S. fell. Sold by BYD. Its rival is Kia. Hyundai grew.") == [
        "Sales in the U.S. fell.", "Sold by BYD.", "Its rival is Kia.", "Hyundai grew."]


def test_a_name_cut_at_an_abbreviation_is_whole_again_in_a_window():
    text = "BYD Co. Ltd. sold 4.27 million vehicles."
    assert sentences_of(text) == ["BYD Co.", "Ltd. sold 4.27 million vehicles."]
    assert text in [s.text for s in candidate_spans([passage(text)])]


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
        Span(first, s[0], 0, 0), Span(first, f"{s[0]} {s[1]}", 0, 1),
        Span(first, s[1], 1, 1), Span(first, f"{s[1]} {s[2]}", 1, 2),
        Span(first, s[2], 2, 2),
        Span(second, "One sentence only.", 0, 0)]


def test_spans_overlap_only_within_one_passage_when_they_share_a_sentence():
    a, b = passage(PARAGRAPH), passage(PARAGRAPH, index=1)
    assert Span(a, "x", 0, 1).overlaps(Span(a, "y", 1, 2))
    assert not Span(a, "x", 0, 0).overlaps(Span(a, "y", 1, 2))
    assert not Span(a, "x", 0, 1).overlaps(Span(b, "y", 0, 1))
    assert Span(a, "x", 0, 2).contains(Span(a, "y", 1, 1))
    assert not Span(a, "x", 0, 1).contains(Span(a, "y", 1, 2))
    assert not Span(a, "x", 0, 2).contains(Span(b, "y", 1, 1))


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


# ---- T18 CS3: a drafting batch split by Passage count ------------------------------------------

def test_a_batch_is_split_by_passage_count_keeping_one_passages_windows_together():
    from insightforge_agent.domain.models import Passage
    from insightforge_agent.domain.passages import Window, split_by_passages

    p = [Passage(observation_id="o", index=i, text=f"t{i}") for i in range(4)]
    batch = [Window(p[0], "a"), Window(p[1], "b1", 1, 2), Window(p[1], "b2", 2, 2),
             Window(p[2], "c"), Window(p[3], "d")]
    parts = split_by_passages(batch, 2)
    assert [[w.text for w in part] for part in parts] == [["a", "b1", "b2"], ["c", "d"]]
    assert split_by_passages(batch, None) == [batch]


def test_t18_drafting_inputs_batch_as_recorded_and_as_the_cap_options_were_costed():
    """Golden: the exact T18 drafting inputs (Passage lengths only; the token estimate depends
    on length alone) give the 144 draft calls T18 made. The cap options were costed at 210 and
    244 calls (analysis-v3/rebatch_out.txt) by counting windows; keeping one Passage's windows
    together, as split_by_passages does, saves one call at 150."""
    import json
    from pathlib import Path

    from insightforge_agent.domain.models import Passage
    from insightforge_agent.domain.passages import batch_passages, split_by_passages

    doc = json.loads((Path(__file__).parents[1] / "fixtures" / "t18" /
                      "drafting_passage_lengths.json").read_text())

    def calls(cap):
        n = 0
        for st in doc["subtasks"]:
            ps = [Passage(observation_id="o", index=i, text=("x " * length)[:length].rstrip() or "x")
                  if length > 1 else Passage(observation_id="o", index=i, text="x")
                  for i, length in enumerate(st["lengths"])]
            n += sum(len(split_by_passages(b, cap)) for b in batch_passages(ps, 3000))
        return n

    assert (calls(None), calls(150), calls(120)) == (144, 209, 244)
