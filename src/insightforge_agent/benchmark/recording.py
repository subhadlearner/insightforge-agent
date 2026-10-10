"""Recording store: normalised Verdicts and raw replies, kept in separate append-only JSONL files.

Logical identity is (candidate, case_id, run). The candidate version, model and safe
configuration are part of the record, so re-recording under the same identity with anything
different fails. Ground truth never enters this store: a record holds a Verdict, which has no
expected label, and the case's content hash covers the prediction-facing view only."""

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from insightforge_agent.benchmark.cases import PredictionCase
from insightforge_agent.benchmark.judge import Scalar, Verdict

REDACTED = "[REDACTED]"

# Explicit allow-list: any configuration field not named here is dropped, not serialised.
SAFE_CONFIG_FIELDS = frozenset({
    "provider", "temperature", "top_p", "seed", "max_output_tokens", "max_attempts", "thinking",
    "prompt_hash", "schema_hash", "device", "dtype", "calibration_temperature",
})

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b(\s*[=:]\s*)\S+"),
]

# Credential fields written as quoted JSON (or Python-dict) keys, possibly escaped inside another
# string. The key must end with a credential word, so usage counters such as "input_tokens" and
# "max_tokens" are left alone. The whole value, quoted or bare, is replaced.
_JSON_CREDENTIAL = re.compile(
    r"""(?P<key>\\?["'][\w.-]*?(?:api[_-]?key|secret|token|password|passwd|authorization"""
    r"""|credentials?|private[_-]?key)\\?["']\s*:\s*)"""
    r"""(?:\\?"(?:[^"\\]|\\.)*?\\?"|\\?'(?:[^'\\]|\\.)*?\\?'|[^\s,}\]]+)""",
    re.IGNORECASE,
)


class RecordingError(Exception):
    pass


class ConflictingRecording(RecordingError):
    pass


class StaleRecording(RecordingError):
    """A recording exists but the case, candidate version, model or configuration has changed."""


def redact_text(text: str, known_secrets: tuple[str, ...] = ()) -> str:
    for secret in known_secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _JSON_CREDENTIAL.sub(lambda m: f'{m.group("key")}"{REDACTED}"', text)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def sanitize_config(
    config: Mapping[str, object], known_secrets: tuple[str, ...] = ()
) -> tuple[dict[str, Scalar], list[str]]:
    """Return (safe fields, names of the fields dropped)."""
    safe: dict[str, Scalar] = {}
    dropped: list[str] = []
    for name in sorted(config):
        value = config[name]
        if name in SAFE_CONFIG_FIELDS and isinstance(value, str | int | float | bool):
            safe[name] = redact_text(value, known_secrets) if isinstance(value, str) else value
        else:
            dropped.append(redact_text(name, known_secrets))
    return safe, dropped


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def case_hash(case: PredictionCase) -> str:
    return content_hash(case.model_dump(mode="json"))


class RunIdentity(BaseModel):
    """Who produced the predictions. `config` is filtered to safe fields when recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: str
    candidate_version: str
    model: str
    run: str
    config: dict[str, object] = {}


class VerdictRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: str
    candidate_version: str
    model: str
    config: dict[str, Scalar]
    config_dropped: list[str]
    config_hash: str
    run: str
    case_id: str
    case_hash: str
    verdict: Verdict


class RawRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: str
    candidate_version: str
    run: str
    case_id: str
    raw_reply: str
    raw_hash: str


Key = tuple[str, str, str]  # (candidate, case_id, run)


def _sanitize_verdict(verdict: Verdict, known: tuple[str, ...]) -> Verdict:
    diagnostics = {
        redact_text(k, known): redact_text(v, known) if isinstance(v, str) else v
        for k, v in verdict.diagnostics.items()
    }
    reason = redact_text(verdict.reason, known) if verdict.reason else verdict.reason
    return verdict.model_copy(update={"reason": reason, "diagnostics": diagnostics})


class RecordingStore:
    """Directory holding `verdicts.jsonl` (normalised) and `raw.jsonl` (raw replies)."""

    def __init__(self, directory: Path, known_secrets: tuple[str, ...] = ()):
        self._dir = directory
        self._known = known_secrets
        directory.mkdir(parents=True, exist_ok=True)
        self._verdicts: dict[Key, VerdictRecord] = self._load("verdicts.jsonl", VerdictRecord)
        self._raw: dict[Key, RawRecord] = self._load("raw.jsonl", RawRecord)

    def _load(self, name: str, model: type) -> dict:
        path = self._dir / name
        records: dict[Key, BaseModel] = {}
        if not path.exists():
            return records
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = model.model_validate_json(line)
            except ValueError as exc:
                raise RecordingError(f"{name} line {number} is unreadable: {exc}") from exc
            key = (record.candidate, record.case_id, record.run)
            if key in records:
                raise RecordingError(f"{name} line {number} repeats {key}")
            records[key] = record
        return records

    def _append(self, name: str, record: BaseModel) -> None:
        with (self._dir / name).open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _clean_identity(self, identity: RunIdentity) -> tuple[dict[str, str], dict, list[str]]:
        config, dropped = sanitize_config(identity.config, self._known)
        clean = {name: redact_text(getattr(identity, name), self._known)
                 for name in ("candidate", "candidate_version", "model", "run")}
        return clean, config, dropped

    def stale_reasons(self, identity: RunIdentity, case: PredictionCase) -> list[str] | None:
        """None if nothing is recorded under this identity's key; otherwise the reasons the
        recording no longer matches (empty list means it is current)."""
        clean, config, _ = self._clean_identity(identity)
        record = self.get(clean["candidate"], case.id, clean["run"])
        if record is None:
            return None
        reasons = []
        if record.case_hash != case_hash(case):
            reasons.append("case changed since it was recorded")
        if record.candidate_version != clean["candidate_version"]:
            reasons.append(f"candidate version {record.candidate_version!r} != {clean['candidate_version']!r}")
        if record.model != clean["model"]:
            reasons.append(f"model {record.model!r} != {clean['model']!r}")
        if record.config_hash != content_hash(config):
            reasons.append("configuration changed")
        return reasons

    def record(
        self,
        identity: RunIdentity,
        case: PredictionCase,
        verdict: Verdict,
        raw_reply: str | None = None,
    ) -> VerdictRecord:
        """Store a normalised Verdict (and optionally the raw reply). Re-recording identical
        content is a no-op; anything different under the same identity raises."""
        if not (verdict.case_id == case.id and verdict.case_type == case.type):
            raise RecordingError("verdict does not belong to the case")
        clean, config, dropped = self._clean_identity(identity)
        record = VerdictRecord(
            candidate=clean["candidate"], candidate_version=clean["candidate_version"],
            model=clean["model"], config=config, config_dropped=dropped,
            config_hash=content_hash(config), run=clean["run"], case_id=case.id,
            case_hash=case_hash(case), verdict=_sanitize_verdict(verdict, self._known),
        )
        key = (record.candidate, record.case_id, record.run)
        raw = None
        if raw_reply is not None:
            text = redact_text(raw_reply, self._known)
            raw = RawRecord(
                candidate=record.candidate, candidate_version=record.candidate_version,
                run=record.run, case_id=record.case_id, raw_reply=text, raw_hash=content_hash(text),
            )
        existing = self._verdicts.get(key)
        if existing is not None and existing != record:
            raise ConflictingRecording(f"{key} is already recorded with different content")
        existing_raw = self._raw.get(key)
        if raw is not None and existing_raw is not None and existing_raw != raw:
            raise ConflictingRecording(f"{key} already has a different raw reply")
        if existing is None:
            self._append("verdicts.jsonl", record)
            self._verdicts[key] = record
        if raw is not None and existing_raw is None:
            self._append("raw.jsonl", raw)
            self._raw[key] = raw
        return record

    def get(self, candidate: str, case_id: str, run: str) -> VerdictRecord | None:
        return self._verdicts.get((candidate, case_id, run))

    def get_raw(self, candidate: str, case_id: str, run: str) -> RawRecord | None:
        return self._raw.get((candidate, case_id, run))

    def records_for(self, candidate: str, run: str) -> list[VerdictRecord]:
        return [r for (c, _, rn), r in self._verdicts.items() if c == candidate and rn == run]


class ReplayMiss(RecordingError):
    pass


class ReplayJudge:
    """Judge that returns recorded Verdicts and never computes anything."""

    def __init__(self, store: RecordingStore, candidate: str, run: str,
                 identity: RunIdentity | None = None):
        """With `identity`, a recording made by a different candidate version, model or
        configuration is refused as stale."""
        self._store, self._candidate, self._run, self._identity = store, candidate, run, identity

    def judge(self, case: PredictionCase) -> Verdict:
        record = self._store.get(self._candidate, case.id, self._run)
        if record is None:
            raise ReplayMiss(f"no recording for ({self._candidate}, {case.id}, {self._run})")
        if record.case_hash != case_hash(case):
            raise ReplayMiss(f"case {case.id} changed since it was recorded")
        if self._identity is not None:
            reasons = self._store.stale_reasons(self._identity, case)
            if reasons:
                raise ReplayMiss(f"stale recording for {case.id}: {'; '.join(reasons)}")
        return record.verdict
