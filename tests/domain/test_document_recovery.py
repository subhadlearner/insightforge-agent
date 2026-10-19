"""T18 CS2: when a failed Document Reader's chunks may be recovered from its own searches, which
ones, and which company an uploaded document speaks for. Pure functions; the T18 cases come
from the saved evaluation (analysis-v2/doc_thread.txt, analysis-v3/b1_replay_out.txt)."""

import pytest

from insightforge_agent.domain.document_recovery import (
    LIMITATION_LEXICON,
    recoverable,
    recovered_chunks,
)
from insightforge_agent.domain.provenance import common_words, document_entity
from insightforge_agent.pipeline.synthesize import literal_problem

T18_S2 = ("FAILED: doc_search returned only metadata (source file, page, and relevance score) "
          "for 8 chunks from northwind-health-annual-report-2025.pdf, pages 1-3, across two "
          "queries, with no text content. I can't confirm that any of them cover the pipeline, "
          "product stages, or R&D spending, so I'm not listing them as supporting chunks. The "
          "likely candidates are obs_5e2476030808cf39:0, :10, and :7, but they need to be read "
          "directly to verify.")
O = "obs_5e2476030808cf39"
T18_S2_SEARCHES = [[f"{O}:{i}" for i in (0, 10, 7, 3, 1, 4, 6, 2)],
                   [f"{O}:{i}" for i in (0, 10, 4, 7, 1, 6, 9, 5)]]


# ---- which failures are the metadata-only limitation ----------------------------------------

def test_the_t18_s2_failure_is_the_recognised_limitation():
    ok, phrases = recoverable(T18_S2, T18_S2_SEARCHES)
    assert ok and LIMITATION_LEXICON == "DOC_READER_LIMITATION_V2"
    assert phrases == ["returned only metadata", "no text content"]


@pytest.mark.parametrize("reason, searches", [
    ("FAILED: the uploaded file is not an annual report.", T18_S2_SEARCHES),
    ("FAILED: no relevant content in the document.", T18_S2_SEARCHES),
    ("FAILED: these chunks are unrelated to R&D.", T18_S2_SEARCHES),
    (T18_S2, []),                                    # no search: nothing to recover
    (T18_S2, [T18_S2_SEARCHES[0], []]),              # an empty search
    ("RESULT: found it | chunks: obs_1:0", T18_S2_SEARCHES),  # not a failure
], ids=["wrong document", "no relevant content", "unrelated", "no search", "empty search",
        "result"])
def test_other_failures_are_never_recovered(reason, searches):
    assert recoverable(reason, searches)[0] is False


def test_a_relevance_judgment_is_never_recovered_even_beside_the_limitation():
    both = "FAILED: no relevant content, but doc_search returned only metadata without text."
    assert recoverable(both, T18_S2_SEARCHES)[0] is False


# ---- which chunks ----------------------------------------------------------------------------

def test_recovered_chunks_follow_best_rank_then_search_order_without_repeats():
    assert recovered_chunks(T18_S2_SEARCHES) == [
        f"{O}:{i}" for i in (0, 10, 7, 4, 3, 1, 6, 9, 2, 5)]  # R&D chunks 3, 4, 5 included


def test_recovery_is_capped_at_sixteen_chunks():
    many = [[f"o:{i}" for i in range(10)], [f"o:{i}" for i in range(10, 20)]]
    chosen = recovered_chunks(many)
    assert len(chosen) == 16 and chosen[:2] == ["o:0", "o:10"]


# ---- the company an uploaded document speaks for ---------------------------------------------

NORTHWIND = [
    "Northwind Health revenue reached 612 million dollars in fiscal 2025.",
    "Cardiology devices contributed 341 million dollars of revenue in fiscal 2025.",
    "Northwind Health employed 2,140 people at the end of fiscal 2025.",
    "Northwind expects regulatory approval for NW-200 in the first half of 2026.",
    "A single supplier provides the titanium used in orthopaedic implants. Pricing pressure "
    "from hospital purchasing groups reduced average selling prices. Delays to the NW-200 "
    "approval would defer expected cardiology revenue.",
    "Northwind Health expects fiscal 2026 revenue growth of 9 to 11 percent.",
]
FILE = "northwind-health-annual-report-2025.pdf"


def test_a_document_naming_itself_as_its_file_does_has_that_entity():
    assert document_entity(FILE, NORTHWIND) == "Northwind Health"


@pytest.mark.parametrize("filename, chunks", [
    ("acme-annual-report.pdf", NORTHWIND),                              # never names Acme
    ("acme-beta-report.pdf", ["Acme sold more.", "Beta sold less.", "Acme grew.", "Beta fell."]),
    ("annual-report-2025.pdf", ["The Annual Report shows growth.", "This Annual Report ends."]),
], ids=["text never names the file's company", "two candidates tie", "only generic title words"])
def test_no_document_entity_when_it_is_not_established(filename, chunks):
    assert document_entity(filename, chunks) is None


RISK = NORTHWIND[4]
COMMON = common_words(NORTHWIND + ["pricing delays a single supplier"])


def check(statement, chunk, entity="Northwind Health"):
    return literal_problem(statement=statement, entities=[], entity="", passage_text=chunk,
                           common=COMMON, document_entity=entity)


def test_the_documents_own_company_is_accepted_in_its_own_chunks():
    assert check("Northwind Health discloses a risk that a single supplier provides the "
                 "titanium used in its orthopaedic implants.", RISK) is None
    assert check("Northwind Health discloses that delays to the NW-200 approval would defer "
                 "expected cardiology revenue.", RISK) is None


def test_the_document_entity_never_reaches_another_company_or_another_document():
    medtronic = "Medtronic reported revenue of 612 million dollars in fiscal 2025."
    assert check("Northwind Health reported revenue of 612 million dollars in fiscal 2025.",
                 medtronic) == "name not in Passage: ['Health', 'Northwind']"
    assert check("Medtronic discloses a risk that a single supplier provides the titanium.",
                 RISK) == "name not in Passage: ['Medtronic']"
    # No entity (a web Passage, or a document that does not establish one): unchanged.
    assert check("Northwind Health discloses a risk that a single supplier provides the "
                 "titanium used in its orthopaedic implants.", RISK, entity=None) == (
        "name not in Passage: ['Health', 'Northwind']")


def test_figures_are_still_checked_against_the_chunk():
    assert check("Northwind Health revenue reached 999 million dollars in fiscal 2025.",
                 NORTHWIND[1]) == "value not in Passage: ['999']"


def test_only_the_chunk_sentences_the_statement_draws_on_are_checked_for_other_names():
    """A risk chunk holds several sentences; "Pricing" opening another sentence is not a
    company. Words the statement itself writes in lower case are common words."""
    tiny = common_words(NORTHWIND[:4])  # a documents-only Run: "pricing" never lower case
    risk1 = "Northwind Health discloses that a single supplier provides the titanium used."
    risk2 = ("Northwind Health discloses that pricing pressure from hospital purchasing groups "
             "reduced average selling prices.")
    for statement in (risk1, risk2):
        assert literal_problem(statement=statement, entities=[], entity="", passage_text=RISK,
                               common=tiny, document_entity="Northwind Health") is None
    # The sentence it draws on is Medtronic's: no credit. (A chunk that itself names Northwind
    # passes the name check like any Passage; attribution is then the entailment call's.)
    mixed = "Medtronic reported that a single supplier provides the titanium. Its plant is in Cork."
    assert literal_problem(statement=risk1, entities=[], entity="", passage_text=mixed,
                           common=tiny, document_entity="Northwind Health") is not None


# ---- review fix: only the metadata-only limitation itself is recoverable ---------------------

@pytest.mark.parametrize("reason", [
    "FAILED: doc_search raised an error: connection timed out after 30 seconds.",
    "FAILED: doc_search returned an exception, so I cannot verify the chunks.",
    "FAILED: I am not authorized to access this document; permission denied.",
    "FAILED: I cannot verify the R&D figures because the report covers 2019, not 2025.",
    "FAILED: I can't confirm the pipeline stages from these results.",
    "FAILED: the PDF metadata shows it is a draft, so it cannot be used.",
    "FAILED: doc_search returned only metadata, and these chunks are not relevant to R&D.",
    "FAILED: doc_search returned only metadata after a timeout error.",
    "FAILED: the results show no text about the R&D pipeline at all.",
], ids=["tool error", "exception then cannot verify", "authorisation", "cannot verify, other reason",
        "can't confirm alone", "metadata in another sense", "metadata-only but irrelevant",
        "metadata-only after an error", "no text about the topic"])
def test_a_failure_that_is_not_the_metadata_only_limitation_is_never_recovered(reason):
    assert recoverable(reason, T18_S2_SEARCHES) == (False, [])
