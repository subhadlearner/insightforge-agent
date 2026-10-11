"""T7: upload limits, document identity and chunk IDs (pure rules)."""

import pytest

from insightforge_agent.domain.contracts import Brief, PassageRef
from insightforge_agent.domain.documents import (
    MAX_DOCUMENT_BYTES,
    Upload,
    check_uploads,
    chunk_id,
    chunk_ids_in,
)
from insightforge_agent.domain.errors import SubmissionRejected

PDF = b"%PDF-1.4 tiny"


def brief_for(uploads):
    return Brief(text="b", source_toggles={"web": True, "documents": True},
                 document_ids=[u.document_id for u in uploads])


def test_ten_pdfs_are_accepted():
    uploads = [Upload(filename=f"r{n}.pdf", data=PDF + bytes([n])) for n in range(10)]
    check_uploads(brief_for(uploads), uploads)


def test_eleven_pdfs_are_rejected():
    uploads = [Upload(filename=f"r{n}.pdf", data=PDF + bytes([n])) for n in range(11)]
    with pytest.raises(SubmissionRejected, match="at most 10"):
        check_uploads(brief_for(uploads), uploads)


def test_a_pdf_over_twenty_megabytes_is_rejected_and_exactly_twenty_is_accepted():
    exact = Upload(filename="ok.pdf", data=PDF + b"x" * (MAX_DOCUMENT_BYTES - len(PDF)))
    check_uploads(brief_for([exact]), [exact])
    big = Upload(filename="big.pdf", data=exact.data + b"x")
    with pytest.raises(SubmissionRejected, match="big.pdf is larger than 20 MB"):
        check_uploads(brief_for([big]), [big])


def test_a_file_that_is_not_a_pdf_is_rejected():
    doc = Upload(filename="notes.docx", data=b"PK\x03\x04")
    with pytest.raises(SubmissionRejected, match="notes.docx is not a PDF"):
        check_uploads(brief_for([doc]), [doc])


def test_document_ids_must_match_the_uploads():
    a = Upload(filename="a.pdf", data=PDF + b"a")
    with pytest.raises(SubmissionRejected, match="do not match"):
        check_uploads(brief_for([]), [a])
    with pytest.raises(SubmissionRejected, match="do not match"):
        check_uploads(brief_for([a]), [])


def test_document_id_is_derived_from_the_content():
    a = Upload(filename="a.pdf", data=PDF)
    assert a.document_id == Upload(filename="renamed.pdf", data=PDF).document_id
    assert a.document_id != Upload(filename="a.pdf", data=PDF + b"!").document_id


def test_chunk_ids_round_trip_and_are_found_in_a_reply():
    ref = PassageRef(observation_id="obs_0123456789abcdef", index=7)
    other = PassageRef(observation_id="obs_fedcba9876543210", index=0)
    reply = f"RESULT: x | chunks: {chunk_id(ref)}, {chunk_id(other)}, {chunk_id(ref)}, obs_bad:1"
    assert chunk_ids_in(reply) == [ref, other]
