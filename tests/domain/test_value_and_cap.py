"""Literal value matching and the rendered per-call Passage cap."""

import pytest

from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.passages import (
    batch_passages,
    position_label,
    render_batch,
    value_in_text,
)
from insightforge_agent.domain.tokens import estimate_tokens


@pytest.mark.parametrize("value,text,expected", [
    ("4.27 million", "BYD sold 4.27 million vehicles.", True),
    ("4.27 billion", "BYD sold 4.27 million vehicles.", False),         # unsupported magnitude
    ("4.27", "BYD sold 4.27 million vehicles.", True),
    ("4.27 million", "BYD sold 4.27 vehicles, then 3 million more.", False),  # not one run
    ("3.02 million", "up from 3.02 million in 2024", True),
    ("4,270,000", "sold 4270000 cars", True),
    ("$32,000", "cut the price to 32,000 dollars", True),
    ("32,000 dollars", "priced at $32,000", True),
    ("32,000 euros", "priced at $32,000", False),                        # other currency
    ("20%", "prices fell 20 percent", True),
    ("20 percent", "prices fell 20%", True),
    ("20 percent", "prices fell 20 per cent", True),
    ("20%", "20 vehicles were sold", False),
    ("4.27m", "BYD sold 4.27 million vehicles.", False),                  # conservative
    ("Zhang Yong", "CEO Zhang Yong said", True),
    ("Zhang Wei", "CEO Zhang Yong said", False),
    ("", "anything", False),
])
def test_a_structured_value_must_be_stated_with_its_units(value, text, expected):
    assert value_in_text(value, text) is expected


def passage(text, index=0):
    return Passage(observation_id="o", index=index, text=text)


@pytest.mark.parametrize("cap", [6, 8, 12, 20, 40])
@pytest.mark.parametrize("length", list(range(1, 30)) + [60, 100, 160])
def test_every_batch_fits_the_cap_as_rendered_at_every_boundary(cap, length):
    words = [("w" * ((i % 5) + 1)) for i in range(length)]
    passages = [passage(" ".join(words), 0), passage("x" * (4 * cap - 1), 1),
                passage("y" * (4 * cap - 4), 2), passage("z" * (4 * cap), 3)]
    batches = batch_passages(passages, cap)
    for batch in batches:
        assert estimate_tokens(render_batch(batch)) <= cap  # labels and separators included
    flat = [w for b in batches for w in b]
    assert {w.passage for w in flat} == set(passages)
    for p in passages:  # nothing truncated: the pieces carry every word, in order
        text = " ".join(w.text for w in flat if w.passage == p)
        assert text.replace(" ", "") == p.text.replace(" ", "")


def test_raw_text_that_exactly_meets_the_cap_is_split_once_labels_are_counted():
    cap = 20
    text = "x" * (4 * cap - 1)  # estimate_tokens(text) == cap exactly
    assert estimate_tokens(text) == cap
    batches = batch_passages([passage(text)], cap)
    assert len(batches) > 1  # the raw text fits, the rendered "[1] ..." does not
    assert all(estimate_tokens(render_batch(b)) <= cap for b in batches)


def test_a_batch_of_several_windows_is_measured_with_its_own_labels_and_separators():
    cap = 40
    passages = [passage("alpha beta gamma delta epsilon zeta eta theta iota kappa", i)
                for i in range(6)]
    batches = batch_passages(passages, cap)
    assert len(batches) > 1 and any(len(b) > 1 for b in batches)
    for b in batches:
        rendered = render_batch(b, position_label)
        assert rendered.count("\n\n") == len(b) - 1 and estimate_tokens(rendered) <= cap
