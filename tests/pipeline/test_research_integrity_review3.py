"""Regression tests for the final architectural review of PR #77 (head f05ee6a), one block per
correction. At f05ee6a, completeness was checked by word matching, answers were restricted by a
capitalisation rule, over-budget Evidence was rebuilt from its structured fact unverified, and a
summary sentence worded as a research limit skipped the semantic check. Each test is adversarial
and covers both directions: a wrong result the old code accepted (false acceptance) and a right
one it refused (false rejection). Scripted models only: these prove that code takes semantic
decisions from the semantic checks and enforces only objective invariants itself, not how often
a live model errs."""

import json

from langchain_core.messages import AIMessage, SystemMessage

from insightforge_agent.domain.contracts import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    ExtractionResult,
    PassageRef,
    ResearchResults,
)
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.domain.requirements import (
    Requirement,
    RequirementLedger,
    assess,
    decomposition_gaps,
    gap_lines,
)
from insightforge_agent.pipeline.requirements import confirm_answers, derive_requirements
from insightforge_agent.pipeline.write import write_report
from tests.pipeline.test_extraction_synthesis import world  # noqa: F401
from tests.pipeline.test_t6_writer_fact_check import evidence
from tests.scripted import (
    ANSWERS_PREFIX,
    COVERAGE_PREFIX,
    JOINT_PREFIX,
    SHORTEN_PREFIX,
    SUPPORT_PREFIX,
    VERIFY_PREFIX,
    ScriptedChat,
    coverage_verdict,
    support_verdicts,
    verify_verdicts,
)

PLAN = SubTaskPlan(aspects=[Aspect(id="a1", name="Cloud")], sub_tasks=[
    SubTask(id="s1", aspect_id="a1", query="q", source_type="web", time_horizon_months=12,
            priority=1)])
REF = PassageRef(observation_id="o", index=0)


def routed(**handlers) -> ScriptedChat:
    """A light model answering each call by its system prompt: `handlers` maps a prefix to
    `handler(user) -> dict`. Defaults are off (`checks=False`): an unscripted call is an
    unusable reply, so a test sees every judgment it relies on."""
    def reply(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        user = str(messages[-1].content)
        for prefix, handler in handlers.items():
            if system.startswith(prefix):
                return AIMessage(content=json.dumps(handler(user)))
        return AIMessage(content="not json")
    return ScriptedChat(reply=reply, checks=False)


def calls(model: ScriptedChat, prefix: str) -> list[str]:
    return [str(c[-1].content) for c in model.seen if str(c[0].content).startswith(prefix)]


def events(deps, type_, run="run_1"):
    return [e.payload for e in deps.repos.events.read_after("alice", run) if e.type == type_]


# ==== 1. Brief completeness is a semantic check, never word matching =========================

BRIEF = "Assess AWS revenue growth in 2025 and its capital spending on AI infrastructure."
GROWTH = {"requirements": [{"id": "R1", "question": "AWS 2025 revenue growth?",
                            "aspect_ids": ["a1"]}]}
BOTH = {"requirements": GROWTH["requirements"] + [
    {"id": "R2", "question": "AWS capital spending on AI infrastructure?", "aspect_ids": ["a1"]}]}
CAPEX = "its capital spending on AI infrastructure"


def coverage_of(uncovered: dict):
    """A coverage judge: the asks of the text no item covers, keyed by the number of items."""
    return lambda user: coverage_verdict(user, uncovered.get(user.count("\n["), ()))


def test_an_omitted_obligation_is_sent_back_once_and_the_retry_is_kept(make_deps):
    replies = iter([GROWTH, BOTH])
    model = routed(**{"You list the atomic": lambda u: next(replies),
                      COVERAGE_PREFIX: coverage_of({1: (CAPEX,)})})
    ledger = derive_requirements(make_deps(light_model=model), "alice", "run_1", BRIEF, PLAN)
    assert ledger.ids() == {"R1", "R2"} and ledger.uncovered_asks == []
    assert ledger.coverage_failed == "" and decomposition_gaps(ledger) == []
    assert CAPEX in calls(model, "You list the atomic")[1]  # the retry names the omission


def test_an_obligation_still_omitted_after_the_retry_is_reported_as_not_established(make_deps):
    model = routed(**{"You list the atomic": lambda u: GROWTH,
                      COVERAGE_PREFIX: coverage_of({1: (CAPEX,)})})
    ledger = derive_requirements(make_deps(light_model=model), "alice", "run_1", BRIEF, PLAN)
    assert len(calls(model, "You list the atomic")) == 2  # one retry, no more
    assert ledger.uncovered_asks == [CAPEX]
    (line,) = decomposition_gaps(ledger)
    assert f'"{CAPEX}"' in line and "did not establish it" in line


def test_a_ledger_using_every_word_of_the_brief_can_still_omit_what_it_asks(make_deps):
    """False acceptance at f05ee6a: these requirements mention every word of the Brief, so the
    word check found nothing, but they swap the years. The coverage judgment decides."""
    brief = "Compare AWS revenue growth in 2025 with Azure revenue growth in 2024."
    swapped = {"requirements": [
        {"id": "R1", "question": "AWS revenue growth in 2024?", "aspect_ids": ["a1"]},
        {"id": "R2", "question": "Azure revenue growth in 2025?", "aspect_ids": ["a1"]}]}
    asks = ("AWS revenue growth in 2025", "Azure revenue growth in 2024")
    model = routed(**{"You list the atomic": lambda u: swapped,
                      COVERAGE_PREFIX: lambda u: {"asks": [{"quote": a, "covered_by": []}
                                                           for a in asks]}})
    ledger = derive_requirements(make_deps(light_model=model), "alice", "run_1", brief, PLAN)
    assert ledger.uncovered_asks == list(asks)
    assert len(decomposition_gaps(ledger)) == 2


def test_instruction_and_conversational_wording_is_not_an_omission(make_deps):
    """False rejection at f05ee6a: Brief words no requirement repeats ("tell", "deck") were
    reported as possible omissions unless listed. Now the judgment decides, with no list."""
    brief = "Please tell me, using the uploaded deck, about Northwind's pipeline."
    ledger_reply = {"requirements": [{"id": "R1", "question": "What is in Northwind's pipeline?",
                                      "aspect_ids": ["a1"]}]}
    model = routed(**{"You list the atomic": lambda u: ledger_reply,
                      COVERAGE_PREFIX: lambda u: {"asks": [
                          {"quote": "Northwind's pipeline", "covered_by": [1]}]}})
    ledger = derive_requirements(make_deps(light_model=model), "alice", "run_1", brief, PLAN)
    assert ledger.uncovered_asks == [] and decomposition_gaps(ledger) == []
    assert len(calls(model, "You list the atomic")) == 1  # nothing to send back


def test_an_unusable_coverage_check_is_recorded_never_taken_as_complete(make_deps):
    for name, reply in (
            ("not json", None),
            ("no asks", {"asks": []}),
            ("quote not in the brief", {"asks": [{"quote": "Oracle revenue", "covered_by": [1]}]}),
            ("unknown item", {"asks": [{"quote": "AWS revenue growth", "covered_by": [4]}]})):
        handlers = {"You list the atomic": lambda u: GROWTH}
        if reply is not None:
            handlers[COVERAGE_PREFIX] = lambda u, r=reply: r
        ledger = derive_requirements(make_deps(light_model=routed(**handlers)), "alice",
                                     "run_1", BRIEF, PLAN)
        assert ledger.coverage_failed, name
        (line,) = decomposition_gaps(ledger)
        assert "could not be checked" in line, name


# ==== 1. Answers: the split is checked for coverage and every claimed answer verified =========

THREE = RequirementLedger(origin="brief", requirements=[Requirement(
    id="R1", question="What was the 2025 cloud revenue of AWS, Azure and Google Cloud?",
    aspect_ids=["a1"])])


def item(id_: str, statement: str) -> EvidenceItem:
    return EvidenceItem(id=id_, subtask_id="s1", aspect_id="a1", confidence="MEDIUM",
                        statement=statement, supporting=[REF], primary=REF,
                        requirement_ids=["R1"])


AWS = item("ev_aws", "AWS reported cloud revenue of $107.6 billion in 2025.")
OTHERS = item("ev_rest", "Azure revenue was $91 billion and Google Cloud revenue was $58 billion "
                         "in 2025.")


def claims(parts, stated):
    """A proposer: `parts`, and per statement the parts `stated(text)` says it states."""
    def reply(user):
        out = []
        for line in user.split("\n"):
            if line.startswith("[") and "] " in line:
                n, text = line[1:].split("] ", 1)
                ks = stated(text)
                out.append({"statement": int(n), "parts": ks,
                            "excerpt": text.rstrip(".") if ks else ""})
        return {"parts": parts, "answers": out}
    return reply


def status_of(make_deps, model, items, ledger=THREE):
    answers = confirm_answers(make_deps(light_model=model), "alice", "run_1", ledger, items)
    (status,) = assess(ledger, PLAN, ResearchResults(results=[]), ExtractionResult(), items,
                       answers=answers)
    return status, answers


def everything(user):
    return coverage_verdict(user)


def test_a_proposer_that_lumps_the_question_is_overruled_by_the_verifier(make_deps):
    """False acceptance guarded at f05ee6a only by the capitalisation rule: the proposer keeps
    the question whole and calls AWS-only Evidence its answer. The verifier judges the pair."""
    model = routed(**{ANSWERS_PREFIX: claims(["AWS, Azure and Google Cloud"], lambda t: [1]),
                      COVERAGE_PREFIX: everything,
                      VERIFY_PREFIX: lambda u: verify_verdicts(u, lambda p, s: "DOES_NOT_ANSWER")})
    status, answers = status_of(make_deps, model, [AWS])
    assert status.status != "supported" and answers.answers == {"R1": []}
    (pair,) = calls(model, VERIFY_PREFIX)
    assert "Asked: AWS, Azure and Google Cloud" in pair and AWS.statement in pair


def test_a_split_that_drops_part_of_the_question_leaves_that_part_unresolved(make_deps):
    """False acceptance: the proposer's split has only AWS, so AWS-only Evidence would answer
    every part. The coverage check of the split finds the rest, which stays unresolved."""
    model = routed(**{ANSWERS_PREFIX: claims(["AWS"], lambda t: [1] if "AWS" in t else []),
                      COVERAGE_PREFIX: lambda u: coverage_verdict(u, ("Azure", "Google Cloud")),
                      VERIFY_PREFIX: verify_verdicts})
    status, answers = status_of(make_deps, model, [AWS])
    assert status.status == "partial" and status.missing_parts == ["Azure", "Google Cloud"]
    (line,) = gap_lines([status])
    assert "answered only in part" in line and "Azure, Google Cloud" in line


def test_a_verified_split_answered_by_two_items_is_supported(make_deps):
    model = routed(**{ANSWERS_PREFIX: claims(["AWS", "Azure", "Google Cloud"], lambda t: [
                          k for k, p in enumerate(["AWS", "Azure", "Google Cloud"], start=1)
                          if p in t]),
                      COVERAGE_PREFIX: everything, VERIFY_PREFIX: verify_verdicts})
    status, answers = status_of(make_deps, model, [AWS, OTHERS])
    assert status.status == "supported" and gap_lines([status]) == []
    assert len(answers.states["R1"]["ev_rest"]) == 2


def test_an_answer_that_does_not_repeat_the_questions_name_is_not_refused(make_deps):
    """False rejection at f05ee6a: "Northwind" is capitalised mid-question, so the name rule
    refused an uploaded document's "The company's revenue..." though it answers. Now the
    verifier decides."""
    ledger = RequirementLedger(origin="brief", requirements=[Requirement(
        id="R1", question="What was Northwind's 2025 revenue?", aspect_ids=["a1"])])
    own = item("ev_nw", "The company's revenue was $5.1 million in 2025.")
    model = routed(**{ANSWERS_PREFIX: claims(["Northwind's 2025 revenue"], lambda t: [1]),
                      COVERAGE_PREFIX: everything, VERIFY_PREFIX: verify_verdicts})
    status, answers = status_of(make_deps, model, [own], ledger)
    assert status.status == "supported" and answers.answers == {"R1": ["ev_nw"]}


def test_an_unsure_verifier_leaves_the_part_unresolved_and_says_so(make_deps):
    model = routed(**{ANSWERS_PREFIX: claims(["AWS", "Azure", "Google Cloud"], lambda t: [
                          k for k, p in enumerate(["AWS", "Azure", "Google Cloud"], start=1)
                          if p in t]),
                      COVERAGE_PREFIX: everything,
                      VERIFY_PREFIX: lambda u: verify_verdicts(
                          u, lambda p, s: "UNSURE" if p == "Google Cloud" else "ANSWERS")})
    status, answers = status_of(make_deps, model, [AWS, OTHERS])
    assert status.status == "partial" and status.missing_parts == ["Google Cloud"]
    assert answers.unsure == {"R1": 1}
    (line,) = gap_lines([status])
    assert "unsure whether 1 statement(s) answer it" in line


def test_a_malformed_verifier_reply_fails_the_check(make_deps):
    proposer = claims(["AWS"], lambda t: [1])
    for name, verdicts in (
            ("missing", []),
            ("duplicate", [{"pair": 1, "verdict": "ANSWERS"}] * 2),
            ("conflicting", [{"pair": 1, "verdict": "ANSWERS"},
                             {"pair": 1, "verdict": "DOES_NOT_ANSWER"}]),
            ("unknown pair", [{"pair": 1, "verdict": "ANSWERS"}, {"pair": 5, "verdict": "ANSWERS"}]),
            ("unknown verdict", [{"pair": 1, "verdict": "PROBABLY"}])):
        model = routed(**{ANSWERS_PREFIX: proposer, COVERAGE_PREFIX: everything,
                          VERIFY_PREFIX: lambda u, v=verdicts: {"verdicts": v}})
        status, answers = status_of(make_deps, model, [AWS])
        assert answers.failed == ["R1"], name
        assert status.status == "verification_incomplete", name


def test_a_failed_coverage_check_of_the_split_fails_the_answer_check(make_deps):
    model = routed(**{ANSWERS_PREFIX: claims(["AWS"], lambda t: [1]),
                      COVERAGE_PREFIX: lambda u: {"asks": [{"quote": "Oracle", "covered_by": []}]},
                      VERIFY_PREFIX: verify_verdicts})
    status, answers = status_of(make_deps, model, [AWS])
    assert answers.failed == ["R1"] and status.status == "verification_incomplete"
    assert calls(model, VERIFY_PREFIX) == []


# ==== 2. Over-budget Evidence: a shorter wording only when judged equivalent both ways ========

def shortener(short: str, verdict=lambda statement, text: "SUPPORTED") -> ScriptedChat:
    """Proposes `short`; `verdict(statement, text)` judges each direction of equivalence."""
    return routed(**{SHORTEN_PREFIX: lambda u: {"statement": short},
                     SUPPORT_PREFIX: lambda u: {"verdicts": support_verdicts(u, verdict)}})


LONG = item("ev_long", "AWS reported cloud revenue of $107.6 billion in 2025; its 2025 cloud "
                       "revenue was $107.6 billion, AWS reported.")
HEDGED = item("ev_hedge", "AWS is expected to report cloud revenue of $120 billion in 2026, "
                          "according to analysts who follow the company.")


def shortened(make_deps, model, it):
    from insightforge_agent.pipeline.synthesize import _shortened

    deps = make_deps(light_model=model)
    return _shortened(deps, "alice", "run_1", it), deps


def test_an_equivalent_shorter_wording_is_used_with_the_same_id_and_citation(make_deps):
    short = "AWS reported cloud revenue of $107.6 billion in 2025."
    model = shortener(short)
    out, _ = shortened(make_deps, model, LONG)
    assert out.shortened == short and out.statement == LONG.statement
    assert (out.id, out.primary, out.supporting) == (LONG.id, LONG.primary, LONG.supporting)
    both = calls(model, SUPPORT_PREFIX)
    assert both[0].startswith(f"Statement: {short}") and LONG.statement in both[0]
    assert both[1].startswith(f"Statement: {LONG.statement}") and short in both[1]


def test_a_wording_that_drops_a_hedge_is_refused(make_deps):
    """False acceptance at f05ee6a: the structured fact "AWS: cloud revenue $120 billion"
    was shown for an analysts' expectation. Here the shorter wording drops the hedge, the
    statement is not supported by it, and the item stays whole."""
    short = "AWS will report cloud revenue of $120 billion in 2026."

    def faithful(statement, text):
        return "INSUFFICIENT_EVIDENCE" if "expected" in statement and "expected" not in text \
            else "SUPPORTED"
    out, deps = shortened(make_deps, shortener(short, faithful), HEDGED)
    assert out is None
    (refused,) = events(deps, "evidence_compression_refused")
    assert refused["verdicts"] == {"adds_nothing": "SUPPORTED",
                                   "drops_nothing": "INSUFFICIENT_EVIDENCE"}


def test_a_changed_figure_is_refused_even_by_a_permissive_judge(make_deps):
    """The objective invariant still holds: a figure of the statement must be stated by the
    shorter wording, whatever the judge says."""
    out, deps = shortened(make_deps, shortener("AWS reported cloud revenue of $108 billion in "
                                               "2025."), LONG)
    assert out is None
    (refused,) = events(deps, "evidence_compression_refused")
    assert "INSUFFICIENT_EVIDENCE" in refused["verdicts"].values()


def test_no_shorter_wording_or_an_unusable_reply_keeps_the_item_whole(make_deps):
    for name, model in (("not shorter", shortener(LONG.statement + " Indeed.")),
                        ("not json", routed())):
        out, deps = shortened(make_deps, model, LONG)
        assert out is None, name
        assert events(deps, "evidence_compression_refused"), name
        assert calls(model, SUPPORT_PREFIX) == [], name  # nothing to verify


def test_the_bundle_is_shortened_before_anything_is_cut(world):
    from insightforge_agent.pipeline.extract import extract
    from insightforge_agent.pipeline.synthesize import bundle_tokens, synthesize
    from insightforge_agent.pipeline.write import render_bundle
    from tests.pipeline.test_extraction_synthesis import BRIEF as SALES, OWNER, RUN, light

    said = "BYD sold {n}.5 million vehicles in region {n} in 2025; in 2025 BYD sold {n}.5 million vehicles in region {n}."
    world.page("https://a.test/x", "\n\n".join(said.format(n=n) for n in range(1, 5)), cred=0.9)
    plan = world.plan(("s1", "a1", 1))
    research = world.research(s1=["https://a.test/x"])
    base = light(extract_=lambda ps, u: [{"passage_index": n, "statement": t, "entities": ["BYD"]}
                                         for n, t in ps],
                 entail=lambda ps, u: [{"passage": n, "verdict": "SUPPORTED"} for n, _ in ps])

    def reply(messages):
        if str(messages[0].content).startswith(SHORTEN_PREFIX):
            statement = str(messages[-1].content).removeprefix("Statement: ")
            return AIMessage(content=json.dumps({"statement": statement.split(";")[0] + "."}))
        return base._generate(messages).generations[0].message
    world.deps.light_model = ScriptedChat(reply=reply, checks=False)
    scope = research.passage_scope()
    extraction = extract(world.deps, OWNER, RUN, list(scope), scope, plan=plan, research=research)
    full = synthesize(world.deps, OWNER, RUN, SALES, plan, research, extraction)
    world.deps.evidence_budget_tokens = bundle_tokens(full) - 10  # over by a little
    bundle = synthesize(world.deps, OWNER, RUN, SALES, plan, research, extraction)
    items = [i for s in bundle.sections for i in s.items]
    assert len(items) == 4 and bundle_tokens(bundle) <= world.deps.evidence_budget_tokens
    short = [i for i in items if i.shortened]
    assert short and all(i.shortened in render_bundle(bundle) for i in short)
    assert {e["evidence_id"] for e in world.events("evidence_compressed")} == {i.id for i in short}
    assert world.events("evidence_cut") == []


def test_a_requirement_whose_evidence_was_cut_for_the_budget_is_not_no_evidence():
    """Coverage consequence of a cut: the ledger says the verified Evidence was left out, never
    that none passed the support check."""
    ledger = RequirementLedger(origin="brief", requirements=[
        Requirement(id="R1", question="AWS 2025 cloud revenue?", aspect_ids=["a1"])])
    (status,) = assess(ledger, PLAN, ResearchResults(results=[]), ExtractionResult(), [],
                       cut=[["R1"]])
    assert status.status == "budget_omitted"
    (line,) = gap_lines([status])
    assert "left out to fit the Evidence budget" in line and "passed the support check" not in line


def test_the_bundle_renders_a_shortened_item_with_its_id_and_period():
    from insightforge_agent.pipeline.write import render_bundle

    it = LONG.model_copy(update={"as_of_period": "2025", "shortened": "AWS cloud revenue: "
                                                                      "$107.6 billion in 2025."})
    shown = render_bundle(EvidenceBundle(sections=[EvidenceSection(
        aspect_id="a1", heading="Cloud", confidence="MEDIUM", items=[it])]))
    assert ("[ev_long] (MEDIUM; as of 2025; shortened) AWS cloud revenue: $107.6 billion in "
            "2025.") in shown
    assert LONG.statement not in shown


# ==== 3. No wording exempts a summary sentence from the semantic check ======================

BYD = "BYD sold 4.27 million vehicles in 2025."
GAP = "Requirement R2 (BYD battery outlook?): this research did not establish it."


def writer_with(summary) -> ScriptedChat:
    return ScriptedChat(reply=lambda m: AIMessage(content=json.dumps(
        {"title": "T", "summary": summary,
         "sections": [{"heading": "Sales", "claims": [{"text": BYD, "evidence_id": "ev_0"}]}]})))


def judged_against_gap(statement, texts):
    return "SUPPORTED" if statement.startswith("This research did not establish BYD's battery "
                                               "outlook.") and GAP in texts else "INSUFFICIENT_EVIDENCE"


def test_a_limit_sentence_that_slips_in_a_finding_is_judged_and_removed(make_deps):
    """False acceptance at f05ee6a: worded as a research limit and figure-free, this sentence
    skipped the check, though it asserts what analysts expect."""
    smuggled = "This research did not establish why analysts expect BYD's margins to collapse."
    model = routed(**{JOINT_PREFIX: lambda u: {"verdict": "INSUFFICIENT_EVIDENCE", "parts": []}})
    deps = make_deps(light_model=model, writer_model=writer_with(f"{BYD} {smuggled}"))
    _, bundle = evidence(deps, [BYD])
    draft = write_report(deps, "alice", "run_1", "EV sales", bundle, [GAP])
    assert draft.summary == BYD
    (checked,) = events(deps, "summary_checked")
    assert checked["judged"] == 1 and checked["removed"] == [smuggled]


def test_a_faithful_limit_sentence_is_kept_on_the_gap_lines_that_record_it(make_deps):
    limit = "This research did not establish BYD's battery outlook."

    def joint(user):
        from tests.scripted import joint_verdict
        return joint_verdict(user, judged_against_gap)
    model = routed(**{JOINT_PREFIX: joint})
    deps = make_deps(light_model=model, writer_model=writer_with(f"{BYD} {limit}"))
    _, bundle = evidence(deps, [BYD])
    draft = write_report(deps, "alice", "run_1", "EV sales", bundle, [GAP])
    assert draft.summary == f"{BYD} {limit}"
    (shown,) = calls(model, JOINT_PREFIX)
    assert GAP in shown  # the gap line was among the units judged
