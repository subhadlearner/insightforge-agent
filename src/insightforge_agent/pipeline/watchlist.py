"""Watchlist CRUD and Planner-only validation. No Run or checkpoint is created by validation."""

from datetime import timedelta

from langchain_core.messages import HumanMessage, SystemMessage

from insightforge_agent.agents.planner import (
    PLANNER_PROMPT,
    ask_clarification,
    asked_question,
    submit_plan,
    submitted_plan,
)
from insightforge_agent.domain.errors import SubmissionRejected
from insightforge_agent.domain.plan import plan_violations, trim_plan
from insightforge_agent.domain.watchlist import (
    WatchlistConfig,
    WatchlistConflict,
    WatchlistDashboard,
    WatchlistItem,
    WatchlistPatch,
    check_watchlist_brief_change,
)
from insightforge_agent.domain.models import RunState
from insightforge_agent.pipeline.memory import eligible_report_ids


def validate_watchlist_brief(deps, owner_id, config: WatchlistConfig, item_id=None):
    """Reuse the Planner tools, prompt and bounds, with at most one corrective reply.
    Semantic vagueness remains the Planner's judgment, never a keyword/length heuristic.
    Only planning tools are bound: no researcher, search, filesystem or execution tool.
    """
    allowed = ["web"]
    if config.source_toggles.get("memory") and eligible_report_ids(
        deps, owner_id, item_id
    ):
        allowed.append("memory")
    planner = deps.planner_model.bind_tools([submit_plan, ask_clarification])
    prompt = f"PLAN. Allowed source types: {', '.join(allowed)}. Brief: {config.brief}"
    problems = []
    for attempt in range(2):
        messages = [SystemMessage(PLANNER_PROMPT), HumanMessage(prompt)]
        reply = planner.invoke(messages)
        if asked_question([*messages, reply]) is not None:
            raise SubmissionRejected(
                "Vague Brief: provide a concrete scope and time horizon"
            )
        try:
            plan = submitted_plan([reply])
            if attempt:
                plan, _ = trim_plan(plan)
            problems = plan_violations(plan, allowed)
            if not problems:
                return
        except ValueError as e:
            problems = [str(e)]
        prompt = (
            f"PLAN (correction). Brief: {config.brief}. Allowed source types: {', '.join(allowed)}. "
            f"Correct these violations: {'; '.join(problems)}. "
            "Consolidate genuinely related Aspects; never drop an Aspect. Call submit_plan."
        )
    raise SubmissionRejected("; ".join(problems))


class WatchlistService:
    def __init__(self, deps, schedules=None):
        self.deps, self.schedules = deps, schedules

    def create(self, owner_id, config: WatchlistConfig):
        validate_watchlist_brief(self.deps, owner_id, config)
        now = self.deps.now()
        item = WatchlistItem(
            **config.model_dump(),
            owner_id=owner_id,
            id=f"wl_{self.deps.new_id()}",
            created_at=now,
            next_tick_at=now + timedelta(minutes=config.cadence_minutes),
        )
        with self.deps.repos.lock:
            self.deps.repos.watchlists.add(item)
            if self.schedules:
                self.schedules.sync(item)
        return self.dashboard(owner_id, item.id)

    def update(self, owner_id, item_id, patch: WatchlistPatch):
        old = self.deps.repos.watchlists.get(owner_id, item_id)
        if old.revision != patch.revision:
            raise WatchlistConflict("the Watchlist Item changed; reload before editing")
        fields = WatchlistConfig.model_fields
        config = WatchlistConfig.model_validate(
            {**{k: getattr(old, k) for k in fields}, **patch.changes()}
        )
        check_watchlist_brief_change(
            old,
            config,
            self.deps.repos.watchlists.counts(owner_id, item_id)[0],
        )
        if config.brief != old.brief or config.source_toggles != old.source_toggles:
            validate_watchlist_brief(self.deps, owner_id, config, item_id)
        next_tick_at = (
            old.next_tick_at
            if config.cadence_minutes == old.cadence_minutes
            else self.deps.now() + timedelta(minutes=config.cadence_minutes)
        )
        item = WatchlistItem(
            **config.model_dump(),
            owner_id=owner_id,
            id=item_id,
            created_at=old.created_at,
            revision=patch.revision + 1,
            next_tick_at=next_tick_at,
        )
        with self.deps.repos.lock:
            self.deps.repos.watchlists.update(item, patch.revision)
            if self.schedules:
                self.schedules.sync(item)
        return self.dashboard(owner_id, item_id)

    def delete(self, owner_id, item_id):
        with self.deps.repos.lock:
            self.deps.repos.watchlists.delete(owner_id, item_id)
            if self.schedules:
                self.schedules.cancel(owner_id, item_id)

    def dashboard(self, owner_id, item_id):
        with self.deps.repos.lock:
            repo = self.deps.repos.watchlists
            item = repo.get(owner_id, item_id)
            started, complete, failed = repo.counts(owner_id, item_id)
            recent = [
                r
                for r in reversed(self.deps.repos.runs.list_for_owner(owner_id))
                if r.watchlist_item_id == item_id and r.state == RunState.FAILED
            ][:5]
            return WatchlistDashboard(
                item=item,
                runs_started=started,
                completed_runs=complete,
                failed_runs=failed,
                recent_failed_runs=recent,
                baseline=repo.baseline(owner_id, item_id),
            )
