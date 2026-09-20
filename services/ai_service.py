"""Skill analysis: ask a model, then verify every citation it gives.

The model is reached through :class:`LLMProvider`, so the provider and model
are configuration rather than assumptions baked into the code; xAI's grok-3 is
the default. The model name and :data:`~services.prompts.PROMPT_VERSION` are
returned with every result and stored on the report, because a verdict that
cannot be traced to the wording and model that produced it cannot be re-run.

What makes the explainability claim real is the verification step, not the
prompt. A citation is checked against the snapshot the analysis was given: the
file must be one the model was actually shown, and the lines must exist in it.
Citations that fail are dropped, and one retry is spent telling the model what
did not resolve. A skill that still has no citation is reported as not
verified, with the reason saying so. Nothing unverifiable is ever presented as
verified.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError

from models.skill_assessment import SKILL_LEVELS
from schemas.reports import EvidenceSpan
from services.prompts import (
    PROMPT_VERSION, README_PATH, build_analysis_prompt, build_retry_note,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "grok-3"
XAI_BASE_URL = "https://api.x.ai/v1"
REQUEST_TIMEOUT = 120.0
MAX_OUTPUT_TOKENS = 8192
# One analysis, then at most one correction round for citations that did not
# resolve. A model that cites nothing real twice is not going to on a third go,
# and each attempt costs a full prompt.
MAX_ATTEMPTS = 2
# Skills the model may volunteer beyond those claimed; the prompt asks for 5.
MAX_EXTRA_SKILLS = 5
MAX_EVIDENCE_PER_SKILL = 6
# A span wide enough to cover a file is not a citation; the prompt asks for 40.
MAX_EVIDENCE_LINES = 200

UNVERIFIABLE_REASON = ("The evidence cited for this skill did not resolve to real lines in the"
                       " analysed commit, so it is reported as unverified.")
UNASSESSED_REASON = "The model returned no verdict for this claimed skill."


class AnalysisError(Exception):
    """The provider failed, or returned something that is not a usable report."""


# --- Providers ---------------------------------------------------------------

@dataclass
class LLMResponse:
    text: str
    model: str
    # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
    token_usage: dict = field(default_factory=dict)


class LLMProvider(Protocol):
    """What the analysis needs from a model: a name, and one call."""

    @property
    def model_name(self) -> str: ...

    async def complete(self, prompt: str) -> LLMResponse: ...


class XAIProvider:
    """xAI through the OpenAI-compatible SDK.

    The client is built on first use, not at import, so the module can be
    imported and tested without an API key present.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None,
                 base_url: str = XAI_BASE_URL, client=None):
        self._api_key = api_key if api_key is not None else os.getenv("XAI_API_KEY")
        self._model = model or os.getenv("AI_MODEL") or DEFAULT_MODEL
        self._base_url = base_url
        self._client = client

    @property
    def model_name(self) -> str:
        return self._model

    def _ensure_client(self):
        if self._client is None:
            if not self._api_key:
                raise AnalysisError("XAI_API_KEY is not set, so no analysis can be run")
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url,
                                       timeout=REQUEST_TIMEOUT)
        return self._client

    async def complete(self, prompt: str) -> LLMResponse:
        client = self._ensure_client()
        try:
            response = await client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
            )
        except AnalysisError:
            raise
        except Exception as error:  # SDK errors are a wide family; the pipeline needs one.
            raise AnalysisError(f"The analysis provider failed: {error}") from error
        choice = response.choices[0] if response.choices else None
        text = (choice.message.content if choice and choice.message else None) or ""
        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=text,
            model=getattr(response, "model", None) or self._model,
            token_usage={
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            } if usage else {},
        )


def get_provider() -> LLMProvider:
    """The configured provider. Only xAI exists today; the protocol is the seam."""
    return XAIProvider()


# --- Reading the model's answer ----------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_response(raw: str) -> dict:
    """The JSON object in a model's reply, however it chose to wrap it."""
    text = (raw or "").strip()
    if not text:
        raise AnalysisError("The analysis provider returned an empty response")
    for candidate in _candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise AnalysisError("The analysis provider did not return a JSON object")


def _candidates(text: str):
    yield text
    fenced = _FENCE.search(text)
    if fenced:
        yield fenced.group(1).strip()
    # Prose either side of the object: take the outermost balanced braces.
    start = text.find("{")
    if start == -1:
        return
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                yield text[start:index + 1]
                return


# --- Checking the citations --------------------------------------------------

def snapshot_line_counts(snapshot) -> dict[str, int]:
    """Every file the model was shown, and how many lines it has.

    The README is counted under the name it was presented as, since the
    snapshot keeps its text but not the path GitHub served it from.
    """
    counts = {path: len(content.splitlines())
              for path, content in (snapshot.sampled_files or {}).items()}
    if snapshot.readme_content:
        counts.setdefault(README_PATH, len(snapshot.readme_content.splitlines()))
    return counts


def validate_evidence(raw_evidence, line_counts: dict[str, int],
                      skill_name: str) -> tuple[list[dict], list[str]]:
    """Split cited evidence into what resolves and why the rest does not."""
    valid: list[dict] = []
    rejected: list[str] = []
    for item in (raw_evidence if isinstance(raw_evidence, list) else [])[:MAX_EVIDENCE_PER_SKILL]:
        if not isinstance(item, dict):
            rejected.append(f"{skill_name}: evidence entry was not an object")
            continue
        try:
            span = EvidenceSpan.model_validate(item)
        except ValidationError as error:
            rejected.append(f"{skill_name}: {_first_message(error)}")
            continue
        if span.file is None:
            valid.append(span.model_dump())
            continue
        path = span.file.lstrip("./")
        available = line_counts.get(path)
        if available is None:
            rejected.append(f"{skill_name}: '{span.file}' is not one of the files you were shown")
            continue
        if span.end_line > available:
            rejected.append(f"{skill_name}: '{path}' has {available} lines, but lines"
                            f" {span.start_line}-{span.end_line} were cited")
            continue
        if span.end_line - span.start_line + 1 > MAX_EVIDENCE_LINES:
            rejected.append(f"{skill_name}: the range cited in '{path}' covers the whole file"
                            " rather than the relevant lines")
            continue
        valid.append({**span.model_dump(), "file": path})
    return valid, rejected


def _first_message(error: ValidationError) -> str:
    first = error.errors()[0]
    return str(first.get("msg", "evidence was not in the required shape")).removeprefix("Value error, ")


# --- Running an analysis -----------------------------------------------------

@dataclass
class AnalysisResult:
    """A finished analysis, ready to become an ``AnalysisReport`` and its skills."""

    overall_trust_score: int | None
    summary: str | None
    # Each entry fills a SkillAssessment: skill_name, claimed, verified, level,
    # confidence, reason, evidence.
    skills: list[dict]
    model_name: str
    prompt_version: str
    raw_response: str
    duration_ms: int
    token_usage: dict
    attempts: int
    # Citations that did not resolve, kept for diagnosis rather than display.
    rejected_citations: list[str]

    def report_fields(self) -> dict:
        """The ``AnalysisReport`` columns this fills."""
        return {
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "overall_trust_score": self.overall_trust_score,
            "summary": self.summary,
            "raw_response": self.raw_response,
            "duration_ms": self.duration_ms,
            "token_usage": self.token_usage,
        }


async def analyze_snapshot(snapshot, skills_claimed: list[str],
                           role_in_project: str | None = None,
                           provider: LLMProvider | None = None) -> AnalysisResult:
    """Assess ``skills_claimed`` against a stored snapshot.

    Raises :class:`AnalysisError` when the provider fails or never returns a
    usable object. A model that cites code which does not exist does not fail
    the run: the citations are dropped and the affected skills are reported as
    unverified.
    """
    provider = provider or get_provider()
    line_counts = snapshot_line_counts(snapshot)
    base_prompt = build_analysis_prompt(snapshot, skills_claimed, role_in_project)

    started = time.monotonic()
    prompt = base_prompt
    last_error: AnalysisError | None = None
    response = parsed = None
    skills: list[dict] = []
    rejected: list[str] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await provider.complete(prompt)
            parsed = parse_response(response.text)
        except AnalysisError as error:
            last_error = error
            logger.warning("Analysis attempt %s did not return a usable report: %s", attempt, error)
            if attempt == MAX_ATTEMPTS:
                raise
            prompt = base_prompt
            continue

        skills, rejected = _assess_skills(parsed, skills_claimed, line_counts)
        if not rejected or attempt == MAX_ATTEMPTS:
            break
        logger.info("Retrying analysis: %s citations did not resolve", len(rejected))
        prompt = base_prompt + build_retry_note(rejected)

    if parsed is None or response is None:  # pragma: no cover - the loop raises first
        raise last_error or AnalysisError("The analysis produced no result")

    return AnalysisResult(
        overall_trust_score=_score(parsed.get("overall_trust_score")),
        summary=_text(parsed.get("summary"), 2000),
        skills=skills,
        model_name=response.model or provider.model_name,
        prompt_version=PROMPT_VERSION,
        raw_response=response.text,
        duration_ms=int((time.monotonic() - started) * 1000),
        token_usage=response.token_usage or {},
        attempts=attempt,
        rejected_citations=rejected,
    )


def _assess_skills(parsed: dict, skills_claimed: list[str],
                   line_counts: dict[str, int]) -> tuple[list[dict], list[str]]:
    """Turn the parsed answer into skill rows, keeping only evidence that resolves."""
    claimed_by_key = {_key(skill): skill for skill in skills_claimed}
    returned = parsed.get("skills")
    assessed: list[dict] = []
    rejected: list[str] = []
    seen: set[str] = set()
    extras = 0

    for item in returned if isinstance(returned, list) else []:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("skill_name"), 50)
        if not name:
            continue
        key = _key(name)
        if key in seen:
            continue
        claimed = key in claimed_by_key
        if not claimed:
            if extras >= MAX_EXTRA_SKILLS:
                continue
            extras += 1
        seen.add(key)

        evidence, failures = validate_evidence(item.get("evidence"), line_counts,
                                               claimed_by_key.get(key, name))
        rejected.extend(failures)
        verified = bool(item.get("verified")) and bool(evidence)
        reason = _text(item.get("reason"), 1000)
        if bool(item.get("verified")) and not evidence:
            # Claimed as demonstrated, but nothing cited survived checking.
            reason = f"{reason} {UNVERIFIABLE_REASON}".strip() if reason else UNVERIFIABLE_REASON
        assessed.append({
            # The submitter's own spelling for a skill they claimed.
            "skill_name": claimed_by_key.get(key, name),
            "claimed": claimed,
            "verified": verified,
            "level": _level(item.get("level")) if verified else None,
            "confidence": _score(item.get("confidence")),
            "reason": reason,
            "evidence": evidence,
        })

    for key, skill in claimed_by_key.items():
        if key not in seen:
            assessed.append({"skill_name": skill, "claimed": True, "verified": False,
                             "level": None, "confidence": None, "reason": UNASSESSED_REASON,
                             "evidence": []})
    return assessed, rejected


def _key(skill: str) -> str:
    return " ".join(str(skill).split()).casefold()


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None


def _score(value) -> int | None:
    """A 0-100 figure, clamped; anything that is not a number is dropped."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(100, int(round(value))))


def _level(value) -> str | None:
    if isinstance(value, str) and value.strip().lower() in SKILL_LEVELS:
        return value.strip().lower()
    return None
