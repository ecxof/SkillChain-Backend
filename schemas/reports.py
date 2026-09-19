from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.analysis_report import ATTESTATION_STATUSES, TIMESTAMP_STATUSES
from models.repo_snapshot import FETCH_MODES
from models.skill_assessment import SKILL_LEVELS

# Evidence found in a particular file must say where in it. The rest describe
# facts about the repository as a whole and have no location.
FILE_EVIDENCE_TYPES = ("source", "manifest", "readme")
REPOSITORY_EVIDENCE_TYPES = ("commits", "languages")
EVIDENCE_TYPES = FILE_EVIDENCE_TYPES + REPOSITORY_EVIDENCE_TYPES


class EvidenceSpan(BaseModel):
    """One piece of evidence behind a skill verdict.

    Used both to parse the model's response and to return stored evidence. This
    checks the span is well formed; whether the cited lines exist in the
    snapshot is checked when the analysis is stored, where the snapshot is at
    hand.
    """

    type: Literal[EVIDENCE_TYPES]
    detail: str = Field(min_length=1, max_length=500)
    file: str | None = Field(default=None, max_length=500)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _location_matches_type(self) -> "EvidenceSpan":
        location = (self.file, self.start_line, self.end_line)
        if self.type in FILE_EVIDENCE_TYPES:
            if any(part is None for part in location):
                raise ValueError(f"{self.type} evidence must cite file, start_line and end_line")
            if self.end_line < self.start_line:
                raise ValueError("end_line must not come before start_line")
        elif any(part is not None for part in location):
            raise ValueError(f"{self.type} evidence describes the repository and cannot cite a file")
        return self


class SkillAssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    skill_name: str
    claimed: bool
    verified: bool
    level: Literal[SKILL_LEVELS] | None
    confidence: int | None = Field(ge=0, le=100)
    reason: str | None
    evidence: list[EvidenceSpan]


AUTHORSHIP_SIGNAL_KINDS = (
    "coauthor_trailers", "commit_sizes", "commit_bursts", "submitter_share", "scaffolding",
)


class AuthorshipSignal(BaseModel):
    """A factual observation about how the repository was developed.

    There is deliberately no score or probability field. A signal states what
    the commit history shows, with the numbers behind it, so the developer can
    check it against the same history; it is never a judgement of AI use.
    """

    kind: Literal[AUTHORSHIP_SIGNAL_KINDS]
    observation: str = Field(min_length=1, max_length=300)
    data: dict[str, int | float | bool | str | list[str]] = Field(default_factory=dict)


class AuthorshipSignalsOut(BaseModel):
    commits_analyzed: int = Field(ge=0)
    signals: list[AuthorshipSignal]


class ProvenanceOut(BaseModel):
    """Everything needed to re-run this analysis and get the same answer."""

    repo_commit_sha: str
    default_branch: str | None
    fetch_mode: Literal[FETCH_MODES]
    fetched_at: datetime
    model_name: str
    prompt_version: str


class AttestationSummaryOut(BaseModel):
    content_hash: str | None
    commit_sha: str | None
    status: Literal[ATTESTATION_STATUSES]
    attested_at: datetime | None
    timestamp_status: Literal[TIMESTAMP_STATUSES]
    anchored_at: datetime | None
    bitcoin_block_height: int | None


class AttestationOut(AttestationSummaryOut):
    """The full proof for independent verification, with no SkillChain account needed."""

    report_id: str
    # The exact bytes that were hashed and committed.
    canonical_json: str | None
    # Base64-encoded OpenTimestamps .ots proof, once one exists.
    ots_proof: str | None
    mirrors: list[str]
    verify_commands: list[str]


class ReportOut(BaseModel):
    """A report as shown to its owner or, for public projects, anyone.

    Excludes raw_response and token_usage, which are internal.
    """

    id: str
    project_id: str
    overall_trust_score: int | None = Field(ge=0, le=100)
    summary: str | None
    skills: list[SkillAssessmentOut]
    authorship: AuthorshipSignalsOut | None
    provenance: ProvenanceOut
    attestation: AttestationSummaryOut
    created_at: datetime

    @classmethod
    def from_report(cls, report) -> "ReportOut":
        """Build from an AnalysisReport with its snapshot and skills loaded."""
        snapshot = report.snapshot
        # Claimed skills first, then alphabetical, so the order is stable.
        skills = sorted(report.skills, key=lambda s: (not s.claimed, s.skill_name.casefold()))
        return cls(
            id=report.id,
            project_id=snapshot.project_id,
            overall_trust_score=report.overall_trust_score,
            summary=report.summary,
            skills=[SkillAssessmentOut.model_validate(skill) for skill in skills],
            authorship=report.authorship_signals,
            provenance=ProvenanceOut(
                repo_commit_sha=snapshot.commit_sha,
                default_branch=snapshot.default_branch,
                fetch_mode=snapshot.fetch_mode,
                fetched_at=snapshot.fetched_at,
                model_name=report.model_name,
                prompt_version=report.prompt_version,
            ),
            attestation=AttestationSummaryOut(
                content_hash=report.content_hash,
                commit_sha=report.attestation_commit_sha,
                status=report.attestation_status,
                attested_at=report.attested_at,
                timestamp_status=report.timestamp_status,
                anchored_at=report.anchored_at,
                bitcoin_block_height=report.bitcoin_block_height,
            ),
            created_at=report.created_at,
        )
