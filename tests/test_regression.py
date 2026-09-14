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


if __name__ == "__main__":
    for test_name in sorted(name for name in globals() if name.startswith("test_")):
        globals()[test_name]()
        print(f"PASS {test_name}")
