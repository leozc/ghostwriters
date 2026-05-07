"""Editor adapter — produces a candidate revision for one iteration (FR3, FR8).

The editor receives the incumbent article plus must-do/must-not-do guardrails,
the most recent iteration's reviewer feedback, and any unconsumed human
notes (one-shot per FR5). It returns a Candidate ready to be scored.

Per FR8, must_do_text and must_not_do_text are passed verbatim into the
prompt as labeled sections; we do not paraphrase or restructure them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ghostwriter.adapters.provider import Provider, prompt_hash
from ghostwriter.types import Candidate, EditorInput, ReviewerOutput


_EDITOR_SYSTEM = (
    "You are a careful copy editor revising an article. Apply the constraints "
    "exactly. Preserve facts. Output only the JSON object specified."
)

_EDITOR_USER_TEMPLATE = """\
MUST DO (verbatim guardrails — apply exactly):
{must_do}

MUST NOT DO (verbatim guardrails — never violate):
{must_not_do}

CURRENT ARTICLE:
<<<ARTICLE
{incumbent}
ARTICLE>>>

PRIOR REVIEWER FEEDBACK (most recent iteration only — may be empty on iter 0):
{prior_reviews}

HUMAN NOTES (one-shot — address these now; will not be passed again):
{human_notes}

Produce a revised article. Respond with ONLY a JSON object:
{{"text": "<full revised article>", "summary": "<1-2 sentences on what you changed and why>"}}\
"""


@dataclass(frozen=True)
class EditorConfig:
    model: str
    system_prompt: str = _EDITOR_SYSTEM
    user_template: str = _EDITOR_USER_TEMPLATE
    max_tokens: int = 4096


def _format_prior_reviews(prior: list[ReviewerOutput]) -> str:
    if not prior:
        return "(none — this is the first iteration)"
    parts = []
    for r in prior:
        rubric_line = ", ".join(f"{k}={v}" for k, v in r.rubric.scores.items())
        parts.append(
            f"- reviewer={r.reviewer_id} weight={r.weight} "
            f"rubric=[{rubric_line}] aggregate={r.rubric.aggregate} "
            f"pairwise={r.pairwise.pref.value}: {r.pairwise.rationale}"
        )
    return "\n".join(parts)


def _format_notes(notes: list[str]) -> str:
    if not notes:
        return "(none)"
    return "\n".join(f"- {n}" for n in notes)


async def propose(
    *,
    config: EditorConfig,
    editor_input: EditorInput,
    provider: Provider,
) -> Candidate:
    """Call the editor for one iteration. Raises ValueError on malformed JSON."""
    user = config.user_template.format(
        must_do=editor_input.must_do_text or "(none)",
        must_not_do=editor_input.must_not_do_text or "(none)",
        incumbent=editor_input.incumbent_text,
        prior_reviews=_format_prior_reviews(editor_input.prior_reviews),
        human_notes=_format_notes(editor_input.human_notes),
    )
    raw = await provider.complete(
        model=config.model,
        system=config.system_prompt,
        user=user,
        max_tokens=config.max_tokens,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"editor returned non-JSON: {raw!r}") from e
    text = payload.get("text")
    summary = payload.get("summary", "")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"editor JSON missing/empty 'text' field: {payload!r}")
    return Candidate(
        text=text,
        edit_summary=summary if isinstance(summary, str) else "",
        prompt_hash=prompt_hash(model=config.model, system=config.system_prompt, user=user),
        model=config.model,
    )
