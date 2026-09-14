from __future__ import annotations

import math
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, ConfigDict

StepId = Union[str, int]
TipId = Union[str, int]
SourceId = Union[str, int]


class ComponentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    volume: float


class InitialLiquidInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    well: str
    volume: float
    components: list[ComponentInput] = Field(default_factory=list)
    # Provenance of the starting liquid. When omitted the liquid is treated as
    # undeclared background volume, which keeps legacy requests unchanged.
    sample_id: Optional[SourceId] = None
    lot_id: Optional[SourceId] = None


class PlateTypeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["rectangular", "custom"] = "rectangular"
    rows: Optional[int] = None
    columns: Optional[int] = None
    well_capacity: float
    custom_wells: Optional[dict[str, float]] = None


class TipStrategyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["new_per_transfer", "reuse_active", "manual"] = "new_per_transfer"
    default_capacity: float
    default_residual_rate: float = 0.0
    default_wash_effectiveness: float = 1.0
    active_tip_id: TipId = "T0"


class StepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StepId
    type: Literal["aspirate", "dispense", "mix", "wash", "change_tip"]
    source: Optional[str] = None
    target: Optional[str] = None
    well: Optional[str] = None
    tip_id: Optional[TipId] = None
    volume: Optional[float] = None
    residual_rate: Optional[float] = None
    wash_effectiveness: Optional[float] = None
    cycles: Optional[int] = None


class TargetThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    well: str
    component: Optional[str] = None
    threshold: float = 1e-6


class AllowedSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A rule matches a packet source when every provided identifier is equal;
    # an empty rule ({}) acts as a wildcard allowing every source.
    sample_id: Optional[SourceId] = None
    lot_id: Optional[SourceId] = None


class SourceThresholdInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    well: str
    allowed_sources: Optional[list[AllowedSourceInput]] = None
    # Maximum share of liquid originating from a non-allowed source.
    max_foreign_fraction: float = 0.0


class ProgramInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "program"
    plate: PlateTypeInput
    initial_liquids: list[InitialLiquidInput] = Field(default_factory=list)
    tip_strategy: TipStrategyInput
    residual_rate: Optional[float] = None
    steps: list[StepInput] = Field(default_factory=list)
    targets: list[TargetThreshold] = Field(default_factory=list)
    source_targets: list[SourceThresholdInput] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: ProgramInput
    targets: list[TargetThreshold] = Field(default_factory=list)
    source_targets: list[SourceThresholdInput] = Field(default_factory=list)
    report_trace: bool = True


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_a: ProgramInput
    version_b: ProgramInput
    targets: list[TargetThreshold] = Field(default_factory=list)
    source_targets: list[SourceThresholdInput] = Field(default_factory=list)
    find_blockers: bool = True
    max_blocker_candidates: int = 40


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _source_identifier(value: Any) -> bool:
    """A provenance identifier is a non-empty string or a non-boolean integer."""
    return (isinstance(value, str) and bool(value.strip())) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


def validation_error(
    code: str,
    message: str,
    field: Optional[str] = None,
    step_id: Optional[StepId] = None,
    step_index: Optional[int] = None,
) -> dict[str, Any]:
    loc: list[Union[str, int]] = []
    if field:
        loc = field.split(".")
    if step_index is not None:
        loc.append(step_index)
    return {
        "code": code,
        "message": message,
        "field": field,
        "step_id": step_id,
        "step_index": step_index,
        "loc": loc,
    }


def plate_wells(plate: PlateTypeInput) -> dict[str, float]:
    if plate.kind == "custom":
        return dict(plate.custom_wells or {})

    rows = int(plate.rows or 0)
    columns = int(plate.columns or 0)
    return {
        f"{chr(ord('A') + r)}{c + 1}": plate.well_capacity
        for r in range(rows)
        for c in range(columns)
    }


def _validate_rate(value: Any, field: str, step: Optional[StepInput] = None, index: Optional[int] = None):
    errors = []
    if value is None:
        return errors
    if not _finite_number(value) or not (0.0 <= float(value) <= 1.0):
        errors.append(
            validation_error(
                "INVALID_RATE",
                f"{field} must be a finite number between 0 and 1.",
                field,
                step.id if step else None,
                index,
            )
        )
    return errors


def validate_program(
    program: ProgramInput,
    extra_targets: Optional[list[TargetThreshold]] = None,
    extra_source_targets: Optional[list["SourceThresholdInput"]] = None,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    plate = program.plate
    wells = plate_wells(plate)

    if plate.kind == "custom":
        if not plate.custom_wells:
            errors.append(validation_error("EMPTY_PLATE", "A custom plate must define custom_wells.", "plate.custom_wells"))
        for well, capacity in (plate.custom_wells or {}).items():
            if not _finite_number(capacity) or capacity <= 0:
                errors.append(
                    validation_error(
                        "INVALID_WELL_CAPACITY",
                        f"Well {well} capacity must be a positive finite number.",
                        f"plate.custom_wells.{well}",
                    )
                )
    else:
        if not _positive_integer(plate.rows):
            errors.append(validation_error("INVALID_PLATE_DIMENSION", "rows must be a positive integer.", "plate.rows"))
        if not _positive_integer(plate.columns):
            errors.append(validation_error("INVALID_PLATE_DIMENSION", "columns must be a positive integer.", "plate.columns"))
        if not _finite_number(plate.well_capacity) or plate.well_capacity <= 0:
            errors.append(validation_error("INVALID_PLATE_CAPACITY", "well_capacity must be positive.", "plate.well_capacity"))

    if not _finite_number(program.tip_strategy.default_capacity) or program.tip_strategy.default_capacity <= 0:
        errors.append(validation_error("INVALID_TIP_CAPACITY", "default_capacity must be positive.", "tip_strategy.default_capacity"))
    errors.extend(_validate_rate(program.tip_strategy.default_residual_rate, "tip_strategy.default_residual_rate"))
    errors.extend(_validate_rate(program.tip_strategy.default_wash_effectiveness, "tip_strategy.default_wash_effectiveness"))
    errors.extend(_validate_rate(program.residual_rate, "residual_rate"))

    seen_initial_wells: set[str] = set()
    for i, initial in enumerate(program.initial_liquids):
        prefix = f"initial_liquids.{i}"
        if initial.well in seen_initial_wells:
            errors.append(validation_error("DUPLICATE_INITIAL_WELL", f"Duplicate initial liquid for well {initial.well}.", f"{prefix}.well"))
        seen_initial_wells.add(initial.well)
        if initial.well not in wells:
            errors.append(validation_error("UNKNOWN_WELL", f"Well {initial.well} does not exist on the plate.", f"{prefix}.well"))
        if not _finite_number(initial.volume) or initial.volume < 0:
            errors.append(validation_error("NEGATIVE_VOLUME", "Initial volume must be non-negative.", f"{prefix}.volume"))
        sample_id = getattr(initial, "sample_id", None)
        lot_id = getattr(initial, "lot_id", None)
        if sample_id is not None and not _source_identifier(sample_id):
            errors.append(
                validation_error(
                    "INVALID_SOURCE_ID",
                    "sample_id must be a non-empty string or an integer.",
                    f"{prefix}.sample_id",
                )
            )
        if lot_id is not None and not _source_identifier(lot_id):
            errors.append(
                validation_error(
                    "INVALID_SOURCE_ID",
                    "lot_id must be a non-empty string or an integer.",
                    f"{prefix}.lot_id",
                )
            )
        component_sum = 0.0
        seen_components: set[str] = set()
        for j, component in enumerate(initial.components):
            cprefix = f"{prefix}.components.{j}"
            if not component.name:
                errors.append(validation_error("EMPTY_COMPONENT", "Component name must not be empty.", f"{cprefix}.name"))
            if component.name in seen_components:
                errors.append(validation_error("DUPLICATE_COMPONENT", f"Duplicate component {component.name}.", f"{cprefix}.name"))
            seen_components.add(component.name)
            if not _finite_number(component.volume) or component.volume < 0:
                errors.append(validation_error("NEGATIVE_VOLUME", f"Component {component.name} volume must be non-negative.", f"{cprefix}.volume"))
            else:
                component_sum += component.volume
        if _finite_number(initial.volume) and initial.components and abs(component_sum - initial.volume) > 1e-9:
            errors.append(
                validation_error(
                    "COMPONENT_SUM_MISMATCH",
                    f"Component volume sum {component_sum} does not equal initial volume {initial.volume}.",
                    f"{prefix}.volume",
                )
            )

    seen_step_ids: set[StepId] = set()
    for i, step in enumerate(program.steps):
        sid = step.id
        if sid in seen_step_ids:
            errors.append(validation_error("DUPLICATE_STEP_ID", f"Duplicate step id {sid}.", f"steps.{i}.id", sid, i))
        seen_step_ids.add(sid)

        def field_error(code: str, message: str, field: str):
            errors.append(validation_error(code, message, f"steps.{i}.{field}", sid, i))

        if step.type in {"aspirate", "dispense", "mix"}:
            if not _finite_number(step.volume) or step.volume is not None and step.volume < 0:
                field_error("NEGATIVE_VOLUME", "volume must be a non-negative finite number.", "volume")
            elif step.volume is not None and step.volume > program.tip_strategy.default_capacity + 1e-9:
                field_error(
                    "TIP_CAPACITY_EXCEEDED",
                    f"Command volume {step.volume} exceeds tip capacity {program.tip_strategy.default_capacity}.",
                    "volume",
                )
            errors.extend(_validate_rate(step.residual_rate, f"steps.{i}.residual_rate", step, i))

        if step.type == "aspirate":
            if not step.source:
                field_error("MISSING_SOURCE", "aspirate requires source.", "source")
            elif step.source not in wells:
                field_error("UNKNOWN_WELL", f"Source well {step.source} does not exist.", "source")
        if step.type == "dispense":
            if not step.target:
                field_error("MISSING_TARGET", "dispense requires target.", "target")
            elif step.target not in wells:
                field_error("UNKNOWN_WELL", f"Target well {step.target} does not exist.", "target")
        if step.type == "mix":
            if not step.well:
                field_error("MISSING_WELL", "mix requires well.", "well")
            elif step.well not in wells:
                field_error("UNKNOWN_WELL", f"Mix well {step.well} does not exist.", "well")
            if step.cycles is not None and (not isinstance(step.cycles, int) or step.cycles <= 0):
                field_error("INVALID_CYCLES", "cycles must be a positive integer.", "cycles")
        if step.type == "wash":
            errors.extend(_validate_rate(step.wash_effectiveness, f"steps.{i}.wash_effectiveness", step, i))
        if step.type in {"wash", "change_tip"} and step.volume is not None:
            field_error("UNEXPECTED_VOLUME", f"{step.type} does not accept volume.", "volume")

    all_targets = list(program.targets) + list(extra_targets or [])
    seen_targets: set[tuple[str, Optional[str]]] = set()
    for i, target in enumerate(all_targets):
        key = (target.well, target.component)
        if key in seen_targets:
            errors.append(validation_error("DUPLICATE_TARGET", f"Duplicate target for {key}.", f"targets.{i}"))
        seen_targets.add(key)
        if target.well not in wells:
            errors.append(validation_error("UNKNOWN_WELL", f"Target well {target.well} does not exist.", f"targets.{i}.well"))
        if not _finite_number(target.threshold) or target.threshold < 0:
            errors.append(validation_error("INVALID_THRESHOLD", "threshold must be non-negative.", f"targets.{i}.threshold"))

    all_source_targets = list(getattr(program, "source_targets", []) or []) + list(extra_source_targets or [])
    seen_source_targets: set[str] = set()
    for i, target in enumerate(all_source_targets):
        if target.well in seen_source_targets:
            errors.append(
                validation_error(
                    "DUPLICATE_SOURCE_TARGET",
                    f"Duplicate source target for well {target.well}.",
                    f"source_targets.{i}",
                )
            )
        seen_source_targets.add(target.well)
        if target.well not in wells:
            errors.append(
                validation_error(
                    "UNKNOWN_WELL",
                    f"Source target well {target.well} does not exist.",
                    f"source_targets.{i}.well",
                )
            )
        if not _finite_number(target.max_foreign_fraction) or not (0.0 <= target.max_foreign_fraction <= 1.0):
            errors.append(
                validation_error(
                    "INVALID_THRESHOLD",
                    "max_foreign_fraction must be a finite number between 0 and 1.",
                    f"source_targets.{i}.max_foreign_fraction",
                )
            )
        if target.allowed_sources is not None:
            for j, allowed in enumerate(target.allowed_sources):
                sample_id = allowed.sample_id
                lot_id = allowed.lot_id
                if sample_id is not None and not _source_identifier(sample_id):
                    errors.append(
                        validation_error(
                            "INVALID_SOURCE_ID",
                            "allowed_sources sample_id must be a non-empty string or an integer.",
                            f"source_targets.{i}.allowed_sources.{j}.sample_id",
                        )
                    )
                if lot_id is not None and not _source_identifier(lot_id):
                    errors.append(
                        validation_error(
                            "INVALID_SOURCE_ID",
                            "allowed_sources lot_id must be a non-empty string or an integer.",
                            f"source_targets.{i}.allowed_sources.{j}.lot_id",
                        )
                    )

    return errors


def merge_source_targets(
    program: ProgramInput,
    extra: Optional[list["SourceThresholdInput"]] = None,
) -> list["SourceThresholdInput"]:
    """Program-level rules win over request-level rules for the same target well."""
    merged: dict[str, "SourceThresholdInput"] = {}
    for target in list(extra or []) + list(getattr(program, "source_targets", []) or []):
        merged[target.well] = target
    return list(merged.values())
