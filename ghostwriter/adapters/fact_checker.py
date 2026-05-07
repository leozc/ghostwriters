"""Fact-checker adapter (FR6).

End-of-loop only. Advisory: never blocks, never reverts. The orchestrator
calls fact_check() on whatever version is the leaf at termination
(TARGET_REACHED, LOOP_LIMIT, or ABORTED).

Output schema:
    {
      "status": "clean" | "has_findings",
      "claims": [
        {"text": "...", "verdict": "verified" | "unverified" | "contradicted" | "subjective",
         "sources": ["..."], "rationale": "..."}
      ]
    }

A "clean" report may have an empty claims list or claims that are all
verified. "has_findings" indicates at least one unverified/contradicted
claim that a human should review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ghostwriter.adapters.provider import Provider, prompt_hash
from ghostwriter.types import (
    ClaimVerdict,
    FactCheckClaim,
    FactCheckReport,
    FactCheckStatus,
)


_FACT_CHECKER_SYSTEM = (
    "You are a fact-checker. Extract concrete factual claims from the article "
    "and assess each one. Mark a claim 'subjective' if it is opinion or "
    "judgement rather than a checkable fact. Output only the JSON object "
    "specified."
)

_FACT_CHECKER_USER_TEMPLATE = """\
ARTICLE TO FACT-CHECK:
<<<ARTICLE
{article}
ARTICLE>>>

Extract every concrete factual claim and assess each. Respond with ONLY
a JSON object:
{{
  "status": "clean" | "has_findings",
  "claims": [
    {{"text": "<claim text>", "verdict": "verified" | "unverified" | "contradicted" | "subjective",
     "sources": ["<url or citation>", ...], "rationale": "<1-2 sentences>"}}
  ]
}}

Mark status='has_findings' if any claim is 'unverified' or 'contradicted'.
Otherwise 'clean'.\
"""


@dataclass(frozen=True)
class FactCheckerConfig:
    model: str
    system_prompt: str = _FACT_CHECKER_SYSTEM
    user_template: str = _FACT_CHECKER_USER_TEMPLATE
    max_tokens: int = 4096


async def fact_check(
    *, config: FactCheckerConfig, article_text: str, provider: Provider
) -> FactCheckReport:
    """One fact-check call. Raises ValueError on malformed JSON."""
    user = config.user_template.format(article=article_text)
    raw = await provider.complete(
        model=config.model,
        system=config.system_prompt,
        user=user,
        max_tokens=config.max_tokens,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"fact-checker returned non-JSON: {raw!r}") from e

    status_raw = payload.get("status")
    if not isinstance(status_raw, str):
        raise ValueError(f"fact-check JSON missing 'status' string: {payload!r}")
    try:
        status = FactCheckStatus(status_raw)
    except ValueError as e:
        raise ValueError(
            f"fact-check 'status' must be clean/has_findings, got {status_raw!r}"
        ) from e

    claims_raw = payload.get("claims", [])
    if not isinstance(claims_raw, list):
        raise ValueError(f"fact-check 'claims' must be a list: {payload!r}")

    claims: list[FactCheckClaim] = []
    for i, c in enumerate(claims_raw):
        if not isinstance(c, dict):
            raise ValueError(f"claim #{i} is not an object: {c!r}")
        text = c.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"claim #{i} missing/empty 'text': {c!r}")
        verdict_raw = c.get("verdict")
        if not isinstance(verdict_raw, str):
            raise ValueError(f"claim #{i} missing 'verdict': {c!r}")
        try:
            verdict = ClaimVerdict(verdict_raw)
        except ValueError as e:
            raise ValueError(
                f"claim #{i} verdict {verdict_raw!r} not in "
                "{verified, unverified, contradicted, subjective}"
            ) from e
        sources_raw = c.get("sources", [])
        if not isinstance(sources_raw, list) or not all(
            isinstance(s, str) for s in sources_raw
        ):
            raise ValueError(f"claim #{i} 'sources' must be list[str]: {c!r}")
        rationale = c.get("rationale", "")
        if not isinstance(rationale, str):
            rationale = ""
        claims.append(
            FactCheckClaim(
                text=text,
                verdict=verdict,
                sources=list(sources_raw),
                rationale=rationale,
            )
        )

    return FactCheckReport(
        status=status,
        claims=claims,
        prompt_hash=prompt_hash(
            model=config.model, system=config.system_prompt, user=user
        ),
        model=config.model,
    )
