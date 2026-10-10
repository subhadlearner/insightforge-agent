"""Regression tests for the behaviour-preserving extractions from T5 `synthesize` (M1.3).

The expected values were captured from the code as it was before each extraction, so they pin
unchanged behaviour rather than the new function's own output."""

import pytest

from insightforge_agent.domain.ids import derive_evidence_id
from insightforge_agent.domain.periods import stated_period
from insightforge_agent.pipeline.synthesize import merge_identity


@pytest.mark.parametrize("fields, identity, evidence_id", [
    (dict(statement="BYD sold 4.27 million.", entity="BYD", predicate="units_sold",
          value="4.27 million", scope="global", asof="2025"),
     "('byd', 'units_sold', 'global')|2025|4270000|", "ev_89883eaf52f1b8c2"),
    # Case, spacing, scope capitalisation and the written form of the number do not matter.
    (dict(statement="x", entity="  byd ", predicate="Units_Sold", value="4,270,000",
          scope=" Global", asof="2025"),
     "('byd', 'units_sold', 'global')|2025|4270000|", "ev_89883eaf52f1b8c2"),
    # A different stated period is a different item.
    (dict(statement="x", entity="BYD", predicate="units_sold", value="4.27 million",
          scope="global", asof="UNKNOWN"),
     "('byd', 'units_sold', 'global')|UNKNOWN|4270000|", "ev_22ef0462dba39e4d"),
    # No structure: keyed by the normalised statement.
    (dict(statement="  Some   Statement, 5%. ", entity="", predicate="", value="", scope="",
          asof="2025"),
     "some statement, 5%.", "ev_cb76879517202618"),
    # A missing value also falls back to the statement.
    (dict(statement="BYD sold   Things", entity="BYD", predicate="units_sold", value="",
          scope="global", asof="2025"),
     "byd sold things", "ev_ab8c41bc63a9ca28"),
])
def test_merge_identity_is_unchanged(fields, identity, evidence_id):
    asof = fields.pop("asof")
    got = merge_identity(**fields, period=stated_period(asof))
    assert got == identity
    assert derive_evidence_id("run1", got) == evidence_id


# ---------------------------------- literal validation ---------------------------------------

from insightforge_agent.pipeline.synthesize import literal_problem  # noqa: E402


@pytest.mark.parametrize("statement, entities, entity, text, problem", [
    ("BYD sold 4.27 million vehicles in 2025.", [], "BYD",
     "BYD sold 4.27 million vehicles in 2025.", None),
    ("BYD sold 5.00 million vehicles.", [], "BYD",
     "BYD sold 4.27 million vehicles in 2025.", "value not in Passage: ['5.00']"),
    ("BYD sold 4.27 million vehicles.", ["Tesla"], "BYD",
     "BYD sold 4.27 million vehicles.", "name not in Passage: ['Tesla']"),
    ("Tesla Motors sold 4.27 million vehicles.", [], "",
     "BYD sold 4.27 million vehicles.", "name not in Passage: ['Motors']"),
    # Names compare case-insensitively; an exact quantity in another form is accepted.
    ("byd sold 4,270,000 vehicles.", [], "byd", "BYD sold 4.27 million vehicles.", None),
    ("Revenue grew 12% in Germany.", [], "", "Revenue grew 12% in Germany.", None),
    ("Revenue grew 12%.", [], "", "Revenue grew 15%.", "value not in Passage: ['12']"),
])
def test_literal_problem_is_unchanged(statement, entities, entity, text, problem):
    assert literal_problem(statement=statement, entities=entities, entity=entity,
                           passage_text=text) == problem
