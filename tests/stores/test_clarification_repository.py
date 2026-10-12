"""T9: the Run repository's clarification compare-and-set, on the fakes and on SQLite."""

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from insightforge_agent.domain.errors import ClarificationConflict, NotFoundError
from insightforge_agent.domain.models import Clarification, Run, RunState
from insightforge_agent.stores.sqlite import SqliteRepos

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
QUESTION = Clarification(question="Which region?", asked_at=T0,
                         deadline=T0 + timedelta(minutes=30))


def add_run(repos, run_id="run_1", owner="alice", state=RunState.PLANNING) -> Run:
    run = Run(id=run_id, owner_id=owner, brief="Tell me about EVs", state=state, created_at=T0)
    repos.runs.add(run)
    return run


def awaiting(repos, run_id="run_1", owner="alice") -> Run:
    add_run(repos, run_id, owner)
    return repos.runs.open_clarification(owner, run_id, QUESTION)


def test_opening_stores_the_question_and_enters_awaiting(repos):
    add_run(repos)
    run = repos.runs.open_clarification("alice", "run_1", QUESTION)
    assert run.state == RunState.AWAITING_CLARIFICATION
    assert repos.runs.get("alice", "run_1").clarification == QUESTION


def test_a_run_asks_at_most_one_question(repos):
    awaiting(repos)
    repos.runs.resolve_clarification("alice", "run_1", answer="EU", resolved_at=T0)
    repos.runs.claim_resume("alice", "run_1")  # back in PLANNING, but it has asked already
    with pytest.raises(ClarificationConflict):
        repos.runs.open_clarification("alice", "run_1", QUESTION)
    assert repos.runs.get("alice", "run_1").clarification.answer == "EU"


def test_only_a_planning_run_can_open_a_question(repos):
    add_run(repos, state=RunState.RESEARCHING)
    with pytest.raises(ClarificationConflict):
        repos.runs.open_clarification("alice", "run_1", QUESTION)


def test_an_answer_is_stored_in_the_same_write_that_enters_resuming(repos):
    awaiting(repos)
    run = repos.runs.resolve_clarification("alice", "run_1", answer="EU only",
                                           resolved_at=T0 + timedelta(minutes=5))
    assert run.state == RunState.RESUMING
    stored = repos.runs.get("alice", "run_1")
    assert stored.state == RunState.RESUMING
    assert stored.clarification.resolution == "answered"
    assert stored.clarification.answer == "EU only"
    assert stored.clarification.resolved_at == T0 + timedelta(minutes=5)
    assert stored.clarification.question == "Which region?"  # the question is kept


def test_a_timeout_is_a_resolution_without_an_answer(repos):
    awaiting(repos)
    run = repos.runs.resolve_clarification("alice", "run_1", answer=None,
                                           resolved_at=QUESTION.deadline)
    assert run.clarification.resolution == "timed_out" and run.clarification.answer is None


def test_the_first_resolver_wins_and_a_second_cannot_overwrite_it(repos):
    awaiting(repos)
    repos.runs.resolve_clarification("alice", "run_1", answer="EU", resolved_at=T0)
    for answer in ("US", None):  # a duplicate answer, then a late timeout
        with pytest.raises(ClarificationConflict):
            repos.runs.resolve_clarification("alice", "run_1", answer=answer, resolved_at=T0)
    stored = repos.runs.get("alice", "run_1")
    assert (stored.clarification.resolution, stored.clarification.answer) == ("answered", "EU")


def test_another_user_cannot_resolve_or_see_the_run(repos):
    awaiting(repos)
    with pytest.raises(NotFoundError):
        repos.runs.resolve_clarification("bob", "run_1", answer="EU", resolved_at=T0)
    assert repos.runs.get("alice", "run_1").state == RunState.AWAITING_CLARIFICATION


def test_exactly_one_caller_claims_a_resolved_run(repos):
    awaiting(repos)
    repos.runs.resolve_clarification("alice", "run_1", answer="EU", resolved_at=T0)
    assert repos.runs.claim_resume("alice", "run_1").state == RunState.PLANNING
    with pytest.raises(ClarificationConflict):
        repos.runs.claim_resume("alice", "run_1")


def test_unfinished_runs_of_every_user_are_listed_and_terminal_ones_are_not(repos):
    for n, state in enumerate(RunState):
        add_run(repos, f"run_{n}", owner="alice" if n % 2 else "bob", state=state)
    listed = {r.state for r in repos.runs.list_unfinished()}
    assert listed == set(RunState) - {RunState.COMPLETE, RunState.FAILED}


# --- SQLite only: real atomicity and an existing database ---------------------------------

def test_concurrent_answer_and_timeout_on_separate_connections_have_one_winner(tmp_path):
    """Two connections to one file, as two threads of the API process would race: whatever
    the interleaving, exactly one resolution is stored and the other caller is refused."""
    path = str(tmp_path / "race.db")
    setup = SqliteRepos(path)
    for n in range(20):
        awaiting(setup, f"run_{n}")
    a, b = SqliteRepos(path), SqliteRepos(path)
    for n in range(20):
        run_id, start, results = f"run_{n}", threading.Barrier(2), []

        def resolve(repos, answer):
            start.wait()
            try:
                repos.runs.resolve_clarification("alice", run_id, answer=answer, resolved_at=T0)
                results.append(answer or "timeout")
            except ClarificationConflict:
                results.append("conflict")

        threads = [threading.Thread(target=resolve, args=(a, "EU")),
                   threading.Thread(target=resolve, args=(b, None))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results).count("conflict") == 1, results
        (winner,) = [r for r in results if r != "conflict"]
        stored = setup.runs.get("alice", run_id).clarification
        assert stored.resolution == ("answered" if winner == "EU" else "timed_out")


def test_concurrent_resolvers_on_one_shared_connection_have_one_winner(tmp_path):
    repos = SqliteRepos(str(tmp_path / "shared.db"))
    awaiting(repos)
    start, results = threading.Barrier(8), []

    def resolve(n):
        start.wait()
        try:
            repos.runs.resolve_clarification("alice", "run_1", answer=f"a{n}", resolved_at=T0)
            results.append(n)
        except ClarificationConflict:
            pass

    threads = [threading.Thread(target=resolve, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1
    assert repos.runs.get("alice", "run_1").clarification.answer == f"a{results[0]}"


def test_a_database_from_before_t9_gains_the_clarification_column(tmp_path):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE runs (owner_id TEXT NOT NULL, id TEXT NOT NULL, brief TEXT NOT "
               "NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, watchlist_item_id TEXT, "
               "PRIMARY KEY (owner_id, id))")
    db.execute("INSERT INTO runs VALUES ('alice','old','b','COMPLETE',?,NULL)",
               (T0.isoformat(),))
    db.commit()
    db.close()
    repos = SqliteRepos(path)
    assert repos.runs.get("alice", "old").clarification is None
    awaiting(repos, "run_new")
    assert repos.runs.get("alice", "run_new").clarification == QUESTION


def test_the_resolution_survives_reopening_the_database(tmp_path):
    path = str(tmp_path / "durable.db")
    first = SqliteRepos(path)
    awaiting(first)
    first.runs.resolve_clarification("alice", "run_1", answer="EU", resolved_at=T0)
    first.close()
    run = SqliteRepos(path).runs.get("alice", "run_1")
    assert run.state == RunState.RESUMING and run.clarification.answer == "EU"
