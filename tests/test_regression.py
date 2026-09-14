from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    """Allow these regression tests to run in environments without FastAPI/Pydantic."""
    try:
        import pydantic  # noqa: F401
        import fastapi  # noqa: F401

        return
    except ModuleNotFoundError:
        pass

    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def model_dump(self):
            return self.__dict__

    def Field(default=None, **_kwargs):
        return [] if default is None else default

    class ConfigDict(dict):
        pass

    pydantic.BaseModel = BaseModel
    pydantic.Field = Field
    pydantic.ConfigDict = ConfigDict
    sys.modules["pydantic"] = pydantic

    fastapi = types.ModuleType("fastapi")

    class FastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, *args, **kwargs):
            return lambda function: function

        def get(self, *args, **kwargs):
            return lambda function: function

        def exception_handler(self, *args, **kwargs):
            return lambda function: function

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.FastAPI = FastAPI
    fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi

    responses = types.ModuleType("fastapi.responses")

    class JSONResponse(Exception):
        def __init__(self, status_code, content):
            self.status_code = status_code
            self.content = content

    responses.JSONResponse = JSONResponse
    sys.modules["fastapi.responses"] = responses


_install_stubs()

from app.main import compare  # noqa: E402
from app.models import validate_program  # noqa: E402
from app.simulator import Simulator, find_minimum_blockers, run_review  # noqa: E402


class TipsStub:
    mode = "reuse_active"
    default_capacity = 100.0
    default_residual_rate = 0.1
    default_wash_effectiveness = 1.0
    active_tip_id = "T"

    def model_dump(self):
        return {
            "mode": self.mode,
            "default_capacity": self.default_capacity,
            "default_residual_rate": self.default_residual_rate,
            "default_wash_effectiveness": self.default_wash_effectiveness,
            "active_tip_id": self.active_tip_id,
        }


PLATE = SimpleNamespace(kind="rectangular", rows=2, columns=2, well_capacity=300.0, custom_wells=None)
TIPS = TipsStub()


def component(name: str, volume: float):
    return SimpleNamespace(name=name, volume=volume)


def initial(well: str, volume: float, name: str):
    return SimpleNamespace(well=well, volume=volume, components=[component(name, volume)])


def initial_source(
    well: str,
    volume: float,
    name: str,
    sample_id=None,
    lot_id=None,
):
    return SimpleNamespace(
        well=well,
        volume=volume,
        components=[component(name, volume)],
        sample_id=sample_id,
        lot_id=lot_id,
    )


def allowed(sample_id=None, lot_id=None):
    return SimpleNamespace(sample_id=sample_id, lot_id=lot_id)


def source_target(well: str, allowed_sources, max_foreign_fraction: float = 0.0):
    return SimpleNamespace(
        well=well,
        allowed_sources=allowed_sources,
        max_foreign_fraction=max_foreign_fraction,
    )


def source_program(name: str, steps, initials):
    return SimpleNamespace(
        name=name,
        plate=PLATE,
        initial_liquids=initials,
        tip_strategy=TIPS,
        residual_rate=None,
        steps=steps,
        targets=[],
        source_targets=[],
    )


# Both samples contain a component with the identical name "DNA". Component-level
# review treats them as the same liquid; provenance review must keep them apart.
SAME_NAME_INITIALS = [
    initial_source("A1", 100.0, "DNA", sample_id="S1"),
    initial_source("A2", 90.0, "DNA", sample_id="S2"),
    initial_source("B1", 100.0, "DNA", sample_id="S1"),
    initial_source("B2", 100.0, "DNA", sample_id="S2"),
]
RULE_B2_S2 = source_target("B2", [allowed(sample_id="S2")])


INITIAL_LIQUIDS = [
    initial("A1", 100.0, "X"),
    initial("A2", 90.0, "Z"),
    initial("B1", 100.0, "Y"),
    initial("B2", 100.0, "W"),
]


def step(step_id, step_type: str, **kwargs):
    data = {
        "id": step_id,
        "type": step_type,
        "source": None,
        "target": None,
        "well": None,
        "tip_id": None,
        "volume": None,
        "residual_rate": None,
        "wash_effectiveness": None,
        "cycles": None,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


BASE_STEPS = [
    step(1, "aspirate", source="A1", volume=100.0, residual_rate=0.1),
    step(2, "dispense", target="B1", volume=100.0, residual_rate=0.1),
    step(3, "aspirate", source="A2", volume=90.0, residual_rate=0.1),
    step(4, "dispense", target="B2", volume=90.0, residual_rate=0.1),
]
FIXED_STEPS = BASE_STEPS[:2] + [step("wash-3", "wash", tip_id="T", wash_effectiveness=1.0)] + BASE_STEPS[2:]
TARGET_B2_X = SimpleNamespace(well="B2", component="X", threshold=0.01)


def make_program(name: str, steps):
    return SimpleNamespace(
        name=name,
        plate=PLATE,
        initial_liquids=INITIAL_LIQUIDS,
        tip_strategy=TIPS,
        residual_rate=None,
        steps=steps,
        targets=[],
    )


def test_single_program_review_keeps_expected_contamination():
    result = run_review(make_program("bad", BASE_STEPS), [TARGET_B2_X])
    target = result["target_results"][0]

    assert target["peak"]["contamination_fraction"] == 0.044751381215469614
    assert target["qualified"] is False
    assert result["final_wells"]["B2"] == {
        "volume": 181.0,
        "capacity": 300.0,
        "components": {"W": 100.0, "X": 8.1, "Z": 72.9},
    }
    assert result["cross_contamination"]["first_by_well_component"][0]["step_id"] == 2


def test_minimum_blocker_is_one_intervention_before_step_3():
    blockers = find_minimum_blockers(make_program("bad", BASE_STEPS), [TARGET_B2_X])

    assert blockers["required_count"] == 1
    assert blockers["actions"] == [
        {
            "before_step_id": 3,
            "step_index": 2,
            "tip_id": "T",
            "action": "change_tip_or_full_wash",
            "recommended_instruction": {"type": "wash", "tip_id": "T", "wash_effectiveness": 1.0},
        }
    ]
    assert blockers["qualified"] is True
    assert blockers["optimality"] == "minimum_strict_block"
    assert blockers["minimum_cut_max_flow"] == 1.0
    assert blockers["calculation_basis"]["max_flow_equals_min_cut"] == 1.0

    verified = Simulator(
        make_program("bad", BASE_STEPS),
        [TARGET_B2_X],
        interventions={3: "clear_tip"},
        report_trace=False,
    ).run()
    assert verified["target_results"][0]["qualified"] is True
    assert "X" not in verified["final_wells"]["B2"]["components"]


def test_compare_returns_threshold_comparison_without_callable_500():
    request = SimpleNamespace(
        version_a=make_program("A", BASE_STEPS),
        version_b=make_program("B", FIXED_STEPS),
        targets=[TARGET_B2_X],
        find_blockers=True,
        max_blocker_candidates=40,
    )

    result = compare(request)
    row = result["target_comparison"][0]

    assert row["well"] == "B2"
    assert row["component"] == "X"
    assert row["threshold"] == 0.01
    assert row["version_a_qualified"] is False
    assert row["version_b_qualified"] is True
    assert row["a_peak_fraction"] == 0.044751381215469614
    assert row["b_peak_fraction"] == 0.0
    assert row["judgment"] == "B_PASSES_A_FAILS"
    assert row["calculation_basis"]["rule"]
    assert result["overall_judgment"] == {
        "a_all_qualified": False,
        "b_all_qualified": True,
        "b_improves_all_failed_targets": True,
    }
    assert result["blockers_for_b"]["required_count"] == 0


def test_input_errors_are_localized_to_fields_and_steps():
    bad_steps = [
        step("dup", "aspirate", source="NOPE", volume=200.0, residual_rate=0.1),
        step("dup", "wash"),
    ]
    bad_program = SimpleNamespace(
        name="bad",
        plate=PLATE,
        initial_liquids=[SimpleNamespace(well="A1", volume=-1.0, components=[])],
        tip_strategy=TIPS,
        residual_rate=None,
        steps=bad_steps,
        targets=[],
    )

    errors = validate_program(bad_program, [])

    assert any(
        error["code"] == "NEGATIVE_VOLUME"
        and error["field"] == "initial_liquids.0.volume"
        for error in errors
    )
    assert any(
        error["code"] == "UNKNOWN_WELL"
        and error["field"] == "steps.0.source"
        and error["step_id"] == "dup"
        and error["step_index"] == 0
        for error in errors
    )
    assert any(
        error["code"] == "TIP_CAPACITY_EXCEEDED"
        and error["field"] == "steps.0.volume"
        and error["step_id"] == "dup"
        for error in errors
    )
    assert any(
        error["code"] == "DUPLICATE_STEP_ID"
        and error["field"] == "steps.1.id"
        and error["step_id"] == "dup"
        and error["step_index"] == 1
        for error in errors
    )


def test_same_named_component_cross_sample_residual_is_caught():
    # Identical "DNA" names: A1(S1) residual rides the reused tip into B2(S2).
    # The component-level checker sees nothing; the provenance rule must fire.
    result = run_review(source_program("bad", BASE_STEPS, SAME_NAME_INITIALS), source_targets=[RULE_B2_S2])

    assert len(result["target_results"]) == 0
    source_result = result["source_target_results"][0]
    assert source_result["well"] == "B2"
    assert source_result["qualified"] is False
    assert source_result["peak"]["foreign_fraction"] == 0.044751381215469614
    assert source_result["peak"]["composition"]["foreign"] == [
        {
            "sample_id": "S1",
            "lot_id": None,
            "volume": 8.1,
            "components": {"DNA": 8.1},
        }
    ]
    first_violation = source_result["first_threshold_violation"]
    assert first_violation["step_id"] == 4
    assert first_violation["step_index"] == 3
    # The complete chain shows the residual riding the reused tip: dispense at
    # step 2 retains S1, it is carried through the step-3 aspirate, then reaches
    # B2 on dispense step 4.
    assert [(event["kind"], event.get("step_id")) for event in source_result["propagation_chain"]] == [
        ("initial", None),
        ("aspirate", 1),
        ("dispense_retained", 2),
        ("aspirate_carryover", 3),
        ("dispense", 4),
    ]
    foreign_chain = source_result["foreign_source_chains"][0]
    assert foreign_chain["sample_id"] == "S1"
    assert foreign_chain["blockable"] is True
    # No component-level cross event exists for same-name "DNA".
    assert result["cross_contamination"]["events"] == []
    source_event = result["cross_contamination"]["source_events"][-1]
    assert source_event["sample_id"] == "S1"
    assert source_event["origin_well"] == "A1"
    assert source_event["target_well"] == "B2"
    # Provenance is also visible in the final well snapshot.
    labels = [entry["sample_id"] for entry in result["final_wells"]["B2"]["source_composition"]]
    assert labels == ["S2", "S1"]


def test_fraction_threshold_allows_limited_same_name_carryover():
    loose_rule = source_target("B2", [allowed(sample_id="S2")], max_foreign_fraction=0.05)
    result = run_review(source_program("ok", BASE_STEPS, SAME_NAME_INITIALS), source_targets=[loose_rule])
    source_result = result["source_target_results"][0]

    assert source_result["peak"]["foreign_fraction"] == 0.044751381215469614
    assert source_result["qualified"] is True
    assert source_result["first_threshold_violation"] is None


def test_mix_propagates_foreign_source_with_full_chain():
    # Tip carries S1 DNA from a prior aspirate; mixing A2 (S2) returns it into
    # the well, and the returned S2 liquid is then pushed around within the well.
    initials = [
        initial_source("A1", 100.0, "DNA", sample_id="S1"),
        initial_source("A2", 100.0, "DNA", sample_id="S2"),
    ]
    mix_steps = [
        step(1, "aspirate", source="A1", volume=50.0, residual_rate=0.1),
        step(2, "mix", well="A2", volume=50.0, cycles=1, residual_rate=0.1),
    ]
    rule = source_target("A2", [allowed(sample_id="S2")], max_foreign_fraction=0.01)
    result = run_review(source_program("mix", mix_steps, initials), source_targets=[rule])
    source_result = result["source_target_results"][0]

    assert source_result["qualified"] is False
    first_violation = source_result["first_threshold_violation"]
    assert first_violation["step_id"] == 2
    assert source_result["first_contamination"]["mechanism"] == "mix_return"
    assert [event["kind"] for event in source_result["propagation_chain"]] == [
        "initial",
        "aspirate",
        "mix_carryover",
        "mix_return",
    ]
    foreign = source_result["peak"]["composition"]["foreign"][0]
    assert foreign["sample_id"] == "S1"
    assert foreign["components"] == {"DNA": foreign["volume"]}
    # Only provenance-level events fire; the component name "DNA" is native to A2.
    assert result["cross_contamination"]["events"] == []
    assert len(result["cross_contamination"]["source_events"]) >= 1


def test_initial_foreign_source_violation_is_unblockable():
    # A2 itself starts with 10% S1 liquid; no mid-program tip action can remove it.
    initials = [
        initial_source("A2", 90.0, "DNA", sample_id="S2"),
        initial_source("A2", 10.0, "DNA", sample_id="S1"),
    ]
    rule = source_target("A2", [allowed(sample_id="S2")])
    program = source_program("initial", [], initials)
    result = run_review(program, source_targets=[rule])
    source_result = result["source_target_results"][0]

    assert source_result["qualified"] is False
    initial = source_result["initial"]
    assert initial["foreign_fraction"] == 0.1
    assert initial["foreign_sources"][0]["sample_id"] == "S1"
    first_violation = source_result["first_threshold_violation"]
    assert first_violation["step_id"] is None
    assert first_violation["step_index"] is None
    assert source_result["blockable"] is False
    assert source_result["propagation_chain"] == [
        {"kind": "initial_foreign_source", "well": "A2", "sample_id": "S1", "lot_id": None}
    ]

    blockers = find_minimum_blockers(program, [], source_targets=[rule])
    assert blockers["optimality"] == "infeasible"
    assert blockers["required_count"] is None
    assert blockers["qualified"] is False


def test_minimum_blocker_clears_same_name_residual_before_step_3():
    blockers = find_minimum_blockers(
        source_program("bad", BASE_STEPS, SAME_NAME_INITIALS),
        [],
        source_targets=[RULE_B2_S2],
    )

    assert blockers["required_count"] == 1
    assert blockers["actions"] == [
        {
            "before_step_id": 3,
            "step_index": 2,
            "tip_id": "T",
            "action": "change_tip_or_full_wash",
            "recommended_instruction": {"type": "wash", "tip_id": "T", "wash_effectiveness": 1.0},
        }
    ]
    assert blockers["qualified"] is True
    assert blockers["minimum_cut_max_flow"] == 1.0


def test_compare_flags_source_rule_and_accepts_wash_fix():
    fixed_steps = (
        BASE_STEPS[:2]
        + [step("wash-3", "wash", tip_id="T", wash_effectiveness=1.0)]
        + BASE_STEPS[2:]
    )
    request = SimpleNamespace(
        version_a=source_program("A", BASE_STEPS, SAME_NAME_INITIALS),
        version_b=source_program("B", fixed_steps, SAME_NAME_INITIALS),
        targets=[],
        source_targets=[RULE_B2_S2],
        find_blockers=True,
        max_blocker_candidates=40,
    )

    result = compare(request)
    row = result["source_comparison"][0]

    assert row["well"] == "B2"
    assert row["version_a_qualified"] is False
    assert row["version_b_qualified"] is True
    assert row["a_peak_foreign_fraction"] == 0.044751381215469614
    assert row["b_peak_foreign_fraction"] == 0.0
    assert row["judgment"] == "B_PASSES_A_FAILS"
    assert result["overall_judgment"] == {
        "a_all_qualified": False,
        "b_all_qualified": True,
        "b_improves_all_failed_targets": True,
    }
    assert result["blockers_for_b"]["required_count"] == 0


def test_legacy_requests_without_source_fields_keep_original_output():
    # No sample_id/lot_id and no source targets: provenance output stays empty.
    result = run_review(make_program("legacy", BASE_STEPS))

    assert result["source_target_results"] == []
    assert result["cross_contamination"]["source_events"] == []
    assert result["cross_contamination"]["first_by_well_source"] == []
    # Final well snapshots carry no provenance block.
    assert "source_composition" not in result["final_wells"]["B2"]
    assert result["summary"]["source_targets_total"] == 0
    assert result["summary"]["all_targets_qualified"] is None


def test_source_rule_validation_errors_are_localized():
    bad_initial = SimpleNamespace(
        well="A1", volume=10.0, components=[], sample_id="", lot_id=None
    )
    program = SimpleNamespace(
        name="bad",
        plate=PLATE,
        initial_liquids=[bad_initial],
        tip_strategy=TIPS,
        residual_rate=None,
        steps=[],
        targets=[],
        source_targets=[source_target("A1", [allowed(sample_id=1)])],
    )
    extra = [
        source_target("A1", []),
        source_target("ZZ", [], max_foreign_fraction=1.5),
    ]

    errors = validate_program(program, extra_source_targets=extra)

    assert any(
        error["code"] == "INVALID_SOURCE_ID"
        and error["field"] == "initial_liquids.0.sample_id"
        for error in errors
    )
    assert any(
        error["code"] == "DUPLICATE_SOURCE_TARGET" and error["field"] == "source_targets.1"
        for error in errors
    )
    assert any(
        error["code"] == "UNKNOWN_WELL" and error["field"] == "source_targets.2.well"
        for error in errors
    )
    assert any(
        error["code"] == "INVALID_THRESHOLD"
        and error["field"] == "source_targets.2.max_foreign_fraction"
        for error in errors
    )


if __name__ == "__main__":
    for test_name in sorted(name for name in globals() if name.startswith("test_")):
        globals()[test_name]()
        print(f"PASS {test_name}")
