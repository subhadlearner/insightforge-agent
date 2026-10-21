"""T15 deterministic Report exports, public projection, JWTs and Chromium PDF seam.

Both exports show the Executive Summary under its own heading and each section's Confidence,
derived from the stored Evidence bundle (`section_confidence`); a section of Historical
background only is labelled as such, never given a current Confidence, and a section whose
label cannot be resolved says so. A Markdown Claim ends
with its Source inline as `[Source: domain, date, credibility: score]` (T20 S11.7)."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import jwt
from pydantic import BaseModel, ValidationError

from insightforge_agent.domain.contracts import (
    EvidenceBundle,
    HistoricalContext,
    ReportDraft,
    SectionLabel,
    VerifiedReport,
    section_confidence,
)
from insightforge_agent.domain.errors import NotFoundError, ProvenanceError
from insightforge_agent.domain.models import Report, Source
from insightforge_agent.domain.sharing import ShareClaims, ShareRecord
from insightforge_agent.stores.source_store import SourceStore

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "insightforge"
JWT_AUDIENCE = "insightforge-public-report"
JWT_REQUIRED = ["exp", "iat", "iss", "aud", "sub", "jti", "owner"]


class ShareTokenError(ValueError):
    pass


class PdfRenderError(RuntimeError):
    pass


class PdfRenderer(Protocol):
    def render(self, report_url: str, *, cookie_name: str, cookie_value: str) -> bytes: ...


class SubprocessPlaywrightRenderer:
    """Render the real Next Report page with the web app's Playwright/Chromium install."""

    def __init__(self, script: Path | None = None, timeout_seconds: int = 60) -> None:
        root = Path(__file__).resolve().parents[3]
        self.script = script or root / "web" / "scripts" / "render-pdf.mjs"
        self.timeout_seconds = timeout_seconds

    def render(self, report_url: str, *, cookie_name: str, cookie_value: str) -> bytes:
        request = json.dumps({
            "url": report_url,
            "cookieName": cookie_name,
            "cookieValue": cookie_value,
        })
        try:
            done = subprocess.run(
                ["node", str(self.script)],
                input=request.encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise PdfRenderError(f"Chromium renderer could not run: {e}") from e
        if done.returncode != 0:
            detail = done.stderr.decode(errors="replace").strip()[-1000:]
            raise PdfRenderError(
                "Chromium renderer failed" + (f": {detail}" if detail else "")
            )
        if not done.stdout.startswith(b"%PDF-"):
            raise PdfRenderError("Chromium renderer did not return a PDF")
        return done.stdout


class ShareTokenService:
    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("share signing secret must have at least 32 characters")
        self.secret = secret

    def issue(self, record: ShareRecord) -> str:
        return jwt.encode(
            {
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
                "sub": record.report_id,
                "jti": record.id,
                "owner": record.owner_id,
                "iat": int(record.created_at.timestamp()),
                "exp": int(record.expires_at.timestamp()),
            },
            self.secret,
            algorithm=JWT_ALGORITHM,
        )

    def verify(self, token: str, now: datetime) -> ShareClaims:
        try:
            payload = jwt.decode(
                token,
                self.secret,
                algorithms=[JWT_ALGORITHM],
                issuer=JWT_ISSUER,
                audience=JWT_AUDIENCE,
                options={
                    "require": JWT_REQUIRED,
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
            expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
            if now >= expires_at:
                raise ShareTokenError("share link expired")
            owner, share_id, report_id = payload["owner"], payload["jti"], payload["sub"]
            if not all(isinstance(v, str) and v for v in (owner, share_id, report_id)):
                raise ShareTokenError("invalid share claims")
            return ShareClaims(
                owner_id=owner, share_id=share_id,
                report_id=report_id, expires_at=expires_at,
            )
        except ShareTokenError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as e:
            raise ShareTokenError("invalid share token") from e


class PublicClaim(BaseModel):
    text: str
    historical: bool = False
    historical_date: str | None = None


class PublicSection(BaseModel):
    heading: str
    confidence: SectionLabel | None = None  # BACKGROUND: Historical only; None: unresolved
    claims: list[PublicClaim]


class PublicWebSource(BaseModel):
    id: str
    title: str
    url: str
    published_at: datetime | None = None
    credibility_score: float | None = None


class PublicFactCheck(BaseModel):
    sampled: int
    verified: int
    unverified: int
    unchecked: int


class PublicReport(BaseModel):
    id: str
    created_at: datetime
    brief: str
    title: str
    summary: str
    sections: list[PublicSection]
    gaps: list[str]
    fact_check: PublicFactCheck
    web_sources: list[PublicWebSource]


def _parts(report: Report):
    try:
        verified = VerifiedReport.model_validate(report.body["verified"])
        evidence = EvidenceBundle.model_validate(report.body["evidence"])
        historical = HistoricalContext.model_validate(report.body.get("historical") or {})
    except (KeyError, ValidationError) as e:
        raise ProvenanceError(f"report {report.id}: malformed body: {e!r}") from e
    refs = {i.id: i.primary for s in evidence.sections for i in s.items}
    refs.update({i.id: i.primary for i in historical.items})
    dates = {i.id: i.report_date.isoformat() for i in historical.items}
    return verified, refs, dates, section_confidence(verified.draft, evidence, historical)


def _claim_sources(
    report: Report, store: SourceStore, owner_id: str
) -> tuple[VerifiedReport, dict[str, str], list[Source]]:
    verified, refs, dates, _ = _parts(report)
    sources: list[Source] = []
    seen: set[str] = set()
    for section in verified.draft.sections:
        for claim in section.claims:
            # ADR-0006: an analytical Claim's premises are Sources it rests on too.
            for eid in ([claim.evidence_id] if claim.historical else claim.evidence_ids()):
                ref = refs.get(eid)
                if ref is None:
                    raise ProvenanceError(f"report {report.id}: claim cites unknown item {eid}")
                try:
                    source = store.source_for_passage(owner_id, ref)
                except NotFoundError as e:
                    raise ProvenanceError(f"report {report.id}: {e}") from e
                if source.id not in seen:
                    sources.append(source)
                    seen.add(source.id)
    return verified, dates, sources


BACKGROUND_ONLY = "Background from earlier Reports only; no current Confidence"
UNRESOLVED = "Confidence unavailable"


def confidence_label(label: SectionLabel | None) -> str:
    if label is None:
        return UNRESOLVED
    return BACKGROUND_ONLY if label == "BACKGROUND" else f"Confidence: {label}"


def inline_source(source: Source) -> str:
    """`[Source: domain, date, credibility: score]`. A document has no domain, so its title is
    shown; a missing date or score is said to be missing, never guessed."""
    where = source.domain or source.title
    date = source.published_at.date().isoformat() if source.published_at else "undated"
    if source.updated_at:  # the page says it was updated since: never its first date alone
        date += f", updated {source.updated_at.date().isoformat()}"
    score = (f"{source.credibility_score:.2f}" if source.credibility_score is not None
             else "not scored")
    return f"[Source: {where}, {date}, credibility: {score}]"


def markdown_export(report: Report, store: SourceStore, owner_id: str) -> str:
    verified, dates, sources = _claim_sources(report, store, owner_id)
    draft: ReportDraft = verified.draft
    lines = [f"# {draft.title}", ""]
    if draft.summary:
        lines += ["## Executive Summary", "", draft.summary, ""]
    source_numbers = {s.id: i + 1 for i, s in enumerate(sources)}
    _, refs, _, confidences = _parts(report)
    for section, confidence in zip(draft.sections, confidences, strict=True):
        lines += [f"## {section.heading}", "", f"*{confidence_label(confidence)}*", ""]
        for claim in section.claims:
            cited = list({source.id: source for source in (
                store.source_for_passage(owner_id, refs[e])
                for e in ([claim.evidence_id] if claim.historical else claim.evidence_ids()))
            }.values())
            historical = (f"Background from an earlier Report of {dates[claim.evidence_id]}: "
                          if claim.historical and claim.evidence_id in dates
                          else "Background from an earlier Report: " if claim.historical else "")
            lines.append(f"- {historical}{claim.text} "
                         + " ".join(inline_source(source) for source in cited))
        lines.append("")
    if draft.gaps:
        lines += ["## Gaps & limitations", ""]
        lines += [f"- {gap}" for gap in draft.gaps]
        lines.append("")
    if sources:
        lines += ["## Sources", ""]
        for source in sources:
            where = source.locator if source.kind == "web" else source.title
            published = (
                f", published {source.published_at.date().isoformat()}"
                if source.published_at else ""
            ) + (f", updated {source.updated_at.date().isoformat()}" if source.updated_at else "")
            lines.append(
                f"{source_numbers[source.id]}. {source.title} — {where}{published}"
            )
        lines.append("")
    return "\n".join(lines)


def public_projection(report: Report, store: SourceStore, owner_id: str) -> PublicReport:
    verified, dates, sources = _claim_sources(report, store, owner_id)
    draft = verified.draft
    confidences = _parts(report)[3]
    return PublicReport(
        id=report.id,
        created_at=report.created_at,
        brief=str(report.body.get("brief") or ""),
        title=draft.title,
        summary=draft.summary,
        sections=[
            PublicSection(
                heading=s.heading,
                confidence=confidence,
                claims=[
                    PublicClaim(
                        text=c.text,
                        historical=c.historical,
                        historical_date=dates.get(c.evidence_id) if c.historical else None,
                    )
                    for c in s.claims
                ],
            )
            for s, confidence in zip(draft.sections, confidences, strict=True)
        ],
        gaps=draft.gaps,
        fact_check=PublicFactCheck(
            sampled=int(verified.summary.get("sampled", 0)),
            verified=int(verified.summary.get("verified", 0)),
            unverified=int(verified.summary.get("unverified", 0)),
            unchecked=int(verified.summary.get("unchecked", 0)),
        ),
        web_sources=[
            PublicWebSource(
                id=s.id, title=s.title, url=s.locator,
                published_at=s.published_at,
                credibility_score=s.credibility_score,
            )
            for s in sources if s.kind == "web"
        ],
    )


def report_url(web_origin: str, report_id: str) -> str:
    return f"{web_origin.rstrip('/')}/reports/{quote(report_id, safe='')}?print=1"
