from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .models import (
    CompareRequest,
    ProgramInput,
    ReviewRequest,
    SourceThresholdInput,
    TargetThreshold,
    merge_source_targets,
    validate_program,
)
from .simulator import find_minimum_blockers, run_review

app = FastAPI(
    title="Pipetting Program Audit API",
    version="1.0.0",
    description="Simulate well volumes, component lots, tip residuals, cross-contamination and minimum interventions.",
)


def request_targets(program: ProgramInput, extra: list[TargetThreshold] | None = None) -> list[TargetThreshold]:
    merged: dict[tuple[str, str | None], TargetThreshold] = {}
    for target in list(program.targets) + list(extra or []):
        merged[(target.well, target.component)] = target
    return list(merged.values())


def request_source_targets(
    program: ProgramInput, extra: list[SourceThresholdInput] | None = None
) -> list[SourceThresholdInput]:
    return merge_source_targets(program, extra)


def raise_validation_errors(errors: list[dict[str, Any]]) -> None:
    raise HTTPException(status_code=422, detail={"error": "VALIDATION_FAILED", "errors": errors})


def audit_program(
    program: ProgramInput,
    extra_targets: list[TargetThreshold] | None = None,
    report_trace: bool = True,
    extra_source_targets: list[SourceThresholdInput] | None = None,
) -> dict[str, Any]:
    errors = validate_program(program, extra_targets, extra_source_targets)
    if errors:
        raise_validation_errors(errors)
    targets = request_targets(program, extra_targets)
    source_targets = request_source_targets(program, extra_source_targets)
    return run_review(program, targets, report_trace=report_trace, source_targets=source_targets)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content=exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/audit")
def audit(request: ReviewRequest) -> dict[str, Any]:
    return audit_program(
        request.program,
        request.targets,
        request.report_trace,
        request.source_targets,
    )


@app.post("/api/programs/audit")
def audit_program_only(program: ProgramInput) -> dict[str, Any]:
    return audit_program(program)


@app.post("/api/programs/compare")
def compare(request: CompareRequest) -> dict[str, Any]:
    request_sources = getattr(request, "source_targets", None) or []
    errors_a = validate_program(request.version_a, request.targets, request_sources)
    errors_b = validate_program(request.version_b, request.targets, request_sources)
    if errors_a or errors_b:
        raise_validation_errors(
            [{"version": "A", **error} for error in errors_a]
            + [{"version": "B", **error} for error in errors_b]
        )

    source_target_keys = request_source_targets(request.version_a, request_sources)
    source_key_list = [t.well for t in source_target_keys]

    result_a = run_review(
        request.version_a,
        request_targets(request.version_a, request.targets),
        source_targets=request_source_targets(request.version_a, request_sources),
    )
    result_b = run_review(
        request.version_b,
        request_targets(request.version_b, request.targets),
        source_targets=request_source_targets(request.version_b, request_sources),
    )

    keys = request_targets(request.version_a, request.targets)
    key_list = [(t.well, t.component) for t in keys]
    map_a = {(t["well"], t["component"]): t for t in result_a["target_results"]}
    map_b = {(t["well"], t["component"]): t for t in result_b["target_results"]}
    target_comparison = []
    for key in key_list:
        a = map_a.get(key)
        b = map_b.get(key)
        target_comparison.append(
            {
                "well": key[0],
                "component": key[1],
                "version_a_qualified": a["qualified"] if a else None,
                "version_b_qualified": b["qualified"] if b else None,
                "a_peak_fraction": a["peak"]["contamination_fraction"] if a and a.get("peak") else 0.0,
                "b_peak_fraction": b["peak"]["contamination_fraction"] if b and b.get("peak") else 0.0,
                "threshold": b["threshold"] if b else a["threshold"],
                "judgment": _comparison_judgment(a, b),
                "calculation_basis": {
                    "rule": "maximum contamination fraction over initial state and all steps <= threshold",
                    "a": a["calculation_basis"] if a else None,
                    "b": b["calculation_basis"] if b else None,
                },
            }
        )

    source_map_a = {t["well"]: t for t in result_a["source_target_results"]}
    source_map_b = {t["well"]: t for t in result_b["source_target_results"]}
    source_comparison = []
    for well in source_key_list:
        a = source_map_a.get(well)
        b = source_map_b.get(well)
        source_comparison.append(
            {
                "well": well,
                "version_a_qualified": a["qualified"] if a else None,
                "version_b_qualified": b["qualified"] if b else None,
                "a_peak_foreign_fraction": (
                    a["peak"]["foreign_fraction"] if a and a.get("peak") else 0.0
                ),
                "b_peak_foreign_fraction": (
                    b["peak"]["foreign_fraction"] if b and b.get("peak") else 0.0
                ),
                "max_foreign_fraction": (
                    b["max_foreign_fraction"] if b else (a["max_foreign_fraction"] if a else 0.0)
                ),
                "allowed_sources": (
                    b["allowed_sources"] if b else (a["allowed_sources"] if a else [])
                ),
                "judgment": _source_comparison_judgment(a, b),
                "calculation_basis": {
                    "rule": "maximum non-allowed-source fraction over initial state and all steps <= max_foreign_fraction",
                    "a": a["calculation_basis"] if a else None,
                    "b": b["calculation_basis"] if b else None,
                },
            }
        )

    blockers_b = None
    if request.find_blockers:
        blockers_b = find_minimum_blockers(
            request.version_b,
            request_targets(request.version_b, request.targets),
            request.max_blocker_candidates,
            request_source_targets(request.version_b, request_sources),
        )

    return {
        "version_a": _compact_result(result_a),
        "version_b": _compact_result(result_b),
        "target_comparison": target_comparison,
        "source_comparison": source_comparison,
        "overall_judgment": {
            "a_all_qualified": bool(result_a["summary"]["all_targets_qualified"]),
            "b_all_qualified": bool(result_b["summary"]["all_targets_qualified"]),
            "b_improves_all_failed_targets": all(
                row["judgment"] != "B_WORSE" for row in target_comparison
            )
            and all(row["judgment"] != "B_WORSE" for row in source_comparison),
        },
        "blockers_for_b": blockers_b,
    }


def _comparison_judgment(a: dict[str, Any] | None, b: dict[str, Any] | None) -> str:
    if not a or not b:
        return "MISSING_TARGET"
    fa = a["peak"]["contamination_fraction"] if a.get("peak") else 0.0
    fb = b["peak"]["contamination_fraction"] if b.get("peak") else 0.0
    if a["qualified"] == b["qualified"] and abs(fa - fb) < 1e-12:
        return "IDENTICAL"
    if b["qualified"] and not a["qualified"]:
        return "B_PASSES_A_FAILS"
    if a["qualified"] and not b["qualified"]:
        return "B_WORSE"
    return "IMPROVED" if fb < fa else "B_WORSE" if fb > fa else "STATUS_CHANGED"


def _source_comparison_judgment(a: dict[str, Any] | None, b: dict[str, Any] | None) -> str:
    if not a or not b:
        return "MISSING_TARGET"
    fa = a["peak"]["foreign_fraction"] if a.get("peak") else 0.0
    fb = b["peak"]["foreign_fraction"] if b.get("peak") else 0.0
    if a["qualified"] == b["qualified"] and abs(fa - fb) < 1e-12:
        return "IDENTICAL"
    if b["qualified"] and not a["qualified"]:
        return "B_PASSES_A_FAILS"
    if a["qualified"] and not b["qualified"]:
        return "B_WORSE"
    return "IMPROVED" if fb < fa else "B_WORSE" if fb > fa else "STATUS_CHANGED"


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": result["program"],
        "summary": result["summary"],
        "findings": result["findings"],
        "cross_contamination": result["cross_contamination"],
        "target_results": result["target_results"],
        "source_target_results": result["source_target_results"],
        "final_wells": result["final_wells"],
        "final_tips": result["final_tips"],
        "trace": result["trace"],
        "metadata": result["metadata"],
    }
