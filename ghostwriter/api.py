"""HTTP API surface for ghostwriter v1.

Endpoint shape mirrors docs/v1-design.md §API:

    POST   /articles                     create from initial draft
    GET    /articles/{id}                article + current incumbent
    POST   /tasks                        create task (TaskConfig in body)
    GET    /tasks/{id}                   status + final_version_id + stop_reason
    GET    /tasks/{id}/lineage           full DAG (FR13)
    POST   /tasks/{id}/iterate           run iterations until stop or N reached
    POST   /tasks/{id}/note              inject one-shot human note
    POST   /tasks/{id}/abort             stop the task
    GET    /tasks/{id}/fact-check-report end-of-loop report (FR6)

Construction is via `create_app(*, store, orchestrator)` so tests can
inject a FakeProvider-backed orchestrator. Production will pin a real
provider once M5.5 lands.

Idempotency-Key handling (v1): the orchestrator's iterate() loop is
self-idempotent via lineage state, so a retried POST /iterate after a
network timeout will not double up; the second call simply continues
from MAX(iter_index)+1. The header is accepted on all POSTs as
forward-compatible scaffolding but is not yet honored for non-iterate
endpoints — document that in the API surface and revisit if a use
case appears.
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from ghostwriter.orchestrator import Orchestrator
from ghostwriter.store.sqlite import Store
from ghostwriter.types import (
    Decision,
    PairwisePref,
    ReviewerSpec,
    StopReason,
    TaskConfig,
    TaskStatus,
)


# =============================================================================
# Request / response schemas
# =============================================================================


class CreateArticleRequest(BaseModel):
    slug: str = Field(min_length=1)
    content: str = Field(min_length=1)


class CreateArticleResponse(BaseModel):
    article_id: str
    version_id: str


class ArticleResponse(BaseModel):
    article_id: str
    slug: str
    incumbent_version_id: str
    incumbent_text: str


class ReviewerSpecModel(BaseModel):
    reviewer_id: str
    weight: float = Field(ge=0)


class CreateTaskRequest(BaseModel):
    article_id: str
    reviewers: list[ReviewerSpecModel]
    target_aggregate: float
    reviewer_floor: float
    loop_limit: int = Field(gt=0)
    must_do_text: str = ""
    must_not_do_text: str = ""


class CreateTaskResponse(BaseModel):
    task_id: str


class TaskResponse(BaseModel):
    task_id: str
    article_id: str
    status: str
    stop_reason: Optional[str] = None
    final_version_id: Optional[str] = None


class IterateRequest(BaseModel):
    max_iterations: Optional[int] = None


class IterateResponse(BaseModel):
    iterations_run: int
    stop_reason: Optional[str] = None
    final_aggregate: Optional[float] = None


class NoteRequest(BaseModel):
    text: str = Field(min_length=1)


class NoteResponse(BaseModel):
    note_id: str


class ReviewerOutputModel(BaseModel):
    reviewer_id: str
    weight: float
    rubric_scores: dict[str, float]
    rubric_aggregate: float
    pairwise_pref: str
    pairwise_rationale: str


class IterationModel(BaseModel):
    id: str
    iter_index: int
    decision: str
    decision_reason: str
    parent_version_id: str
    candidate_id: str
    aggregate_score: float
    pref_delta: float
    pref_ci_low: float
    pref_ci_high: float
    reviewer_outputs: list[ReviewerOutputModel]


class LineageResponse(BaseModel):
    task_id: str
    iterations: list[IterationModel]


class FactCheckClaimModel(BaseModel):
    text: str
    verdict: str
    sources: list[str]
    rationale: str


class FactCheckReportModel(BaseModel):
    status: str
    claims: list[FactCheckClaimModel]


# =============================================================================
# App factory
# =============================================================================


def create_app(*, store: Store, orchestrator: Orchestrator) -> FastAPI:
    app = FastAPI(title="ghostwriter v1")

    # ---- articles ----------------------------------------------------------

    @app.post("/articles", response_model=CreateArticleResponse, status_code=201)
    def create_article(
        body: CreateArticleRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> CreateArticleResponse:
        article_id, version_id = store.create_article(
            slug=body.slug, content=body.content
        )
        return CreateArticleResponse(article_id=article_id, version_id=version_id)

    @app.get("/articles/{article_id}", response_model=ArticleResponse)
    def get_article(article_id: str) -> ArticleResponse:
        art = store.get_article(article_id)
        if art is None:
            raise HTTPException(404, f"article {article_id} not found")
        latest = store.latest_version(article_id)
        if latest is None:
            raise HTTPException(500, f"article {article_id} has no versions")
        return ArticleResponse(
            article_id=article_id,
            slug=art["slug"],
            incumbent_version_id=latest["id"],
            incumbent_text=store.read_version_text(latest["id"]),
        )

    # ---- tasks -------------------------------------------------------------

    @app.post("/tasks", response_model=CreateTaskResponse, status_code=201)
    def create_task(
        body: CreateTaskRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> CreateTaskResponse:
        if store.get_article(body.article_id) is None:
            raise HTTPException(404, f"article {body.article_id} not found")
        cfg = TaskConfig(
            article_id=body.article_id,
            reviewers=[
                ReviewerSpec(reviewer_id=r.reviewer_id, weight=r.weight)
                for r in body.reviewers
            ],
            target_aggregate=body.target_aggregate,
            reviewer_floor=body.reviewer_floor,
            loop_limit=body.loop_limit,
            must_do_text=body.must_do_text,
            must_not_do_text=body.must_not_do_text,
        )
        task_id = store.create_task(cfg)
        return CreateTaskResponse(task_id=task_id)

    @app.get("/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: str) -> TaskResponse:
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id} not found")
        return TaskResponse(
            task_id=task_id,
            article_id=task["article_id"],
            status=task["status"],
            stop_reason=task.get("stop_reason"),
            final_version_id=task.get("final_version_id"),
        )

    @app.get("/tasks/{task_id}/lineage", response_model=LineageResponse)
    def get_lineage(task_id: str) -> LineageResponse:
        if store.get_task(task_id) is None:
            raise HTTPException(404, f"task {task_id} not found")
        records = store.get_lineage(task_id)
        return LineageResponse(
            task_id=task_id,
            iterations=[
                IterationModel(
                    id=r.id,
                    iter_index=r.iter_index,
                    decision=r.decision.value,
                    decision_reason=r.decision_reason,
                    parent_version_id=r.parent_version_id,
                    candidate_id=r.candidate_id,
                    aggregate_score=r.aggregate_score,
                    pref_delta=r.pref_delta,
                    pref_ci_low=r.pref_ci_low,
                    pref_ci_high=r.pref_ci_high,
                    reviewer_outputs=[
                        ReviewerOutputModel(
                            reviewer_id=ro.reviewer_id,
                            weight=ro.weight,
                            rubric_scores=ro.rubric.scores,
                            rubric_aggregate=ro.rubric.aggregate,
                            pairwise_pref=ro.pairwise.pref.value,
                            pairwise_rationale=ro.pairwise.rationale,
                        )
                        for ro in r.reviewer_outputs
                    ],
                )
                for r in records
            ],
        )

    @app.post("/tasks/{task_id}/iterate", response_model=IterateResponse)
    async def iterate(
        task_id: str,
        body: IterateRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> IterateResponse:
        if store.get_task(task_id) is None:
            raise HTTPException(404, f"task {task_id} not found")
        try:
            result = await orchestrator.iterate(
                task_id, max_iterations=body.max_iterations
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return IterateResponse(
            iterations_run=result.iterations_run,
            stop_reason=result.stop_reason.value if result.stop_reason else None,
            final_aggregate=result.final_aggregate,
        )

    @app.post("/tasks/{task_id}/note", response_model=NoteResponse, status_code=201)
    def add_note(
        task_id: str,
        body: NoteRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> NoteResponse:
        if store.get_task(task_id) is None:
            raise HTTPException(404, f"task {task_id} not found")
        note_id = store.add_human_note(task_id, body.text)
        return NoteResponse(note_id=note_id)

    @app.post("/tasks/{task_id}/abort", status_code=204)
    async def abort(
        task_id: str,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> None:
        try:
            await orchestrator.abort(task_id)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get(
        "/tasks/{task_id}/fact-check-report", response_model=FactCheckReportModel
    )
    def get_fact_check(task_id: str) -> FactCheckReportModel:
        if store.get_task(task_id) is None:
            raise HTTPException(404, f"task {task_id} not found")
        report = store.get_fact_check(task_id)
        if report is None:
            raise HTTPException(
                404,
                f"task {task_id} has no fact-check report yet "
                "(only available after termination)",
            )
        return FactCheckReportModel(
            status=report.status.value,
            claims=[
                FactCheckClaimModel(
                    text=c.text,
                    verdict=c.verdict.value,
                    sources=c.sources,
                    rationale=c.rationale,
                )
                for c in report.claims
            ],
        )

    return app
