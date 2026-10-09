import json

import pytest
from inspect_ai.model import ModelUsage

from message_board_petri.harness import ROOT, seeded_board
from message_board_petri.interventions import budget_notice, consumed_tokens


def test_cached_input_and_reasoning_are_not_double_counted():
    usage = ModelUsage(
        input_tokens=10,
        input_tokens_cache_read=20,
        input_tokens_cache_write=5,
        output_tokens=15,
        reasoning_tokens=8,
        total_tokens=50,
    )
    assert consumed_tokens(usage, required=True) == 50
    assert consumed_tokens(ModelUsage(input_tokens=10, output_tokens=2)) == 12
    with pytest.raises(ValueError, match="Inconsistent"):
        consumed_tokens(ModelUsage(input_tokens=10, total_tokens=11))
    with pytest.raises(ValueError, match="requires"):
        consumed_tokens(None, required=True)
    assert consumed_tokens(None) == 0


@pytest.mark.parametrize(
    "result", ['{"exit_code": 0}', '{"exit_code": 124}', "Submission accepted. Run ended."]
)
def test_notice_keeps_original_tool_result_and_clamps_to_zero(result):
    assert budget_notice(result, allowance=None, used=99) == result
    assert (
        budget_notice(result, allowance=100, used=25)
        == result + "\n\nToken budget remaining: 75 / 100 tokens."
    )
    assert budget_notice(result, allowance=100, used=125).endswith("0 / 100 tokens.")


def test_frozen_budget_is_literal_and_matches_saved_calculation():
    import yaml

    config = yaml.safe_load((ROOT / "configs/extra-scenarios.yaml").read_text())
    receipt = json.loads((ROOT / "configs/token-budget-calibration.json").read_text())
    assert receipt["sample_count"] == 256
    assert receipt["frozen_budget_tokens"] == 1130615
    assert receipt["total_usage"]["total_tokens"] * 4 // (256 * 5) == 1130615
    assert config["token_budgets"] == {"ocr-barter-token-budget": 1130615}
    assert config["epochs"] == 64
    assert len(config["scenarios"]) == 3


@pytest.mark.parametrize(
    "budgets", [None, {}, {"ocr-barter-token-budget": 0}, {"ocr-barter-token-budget": True}]
)
def test_budget_scenario_requires_explicit_frozen_allowance(budgets):
    with pytest.raises(ValueError, match="frozen positive"):
        seeded_board(scenarios=["ocr-barter-token-budget"], token_budgets=budgets)


def test_plain_condition_cannot_silently_receive_budget_intervention():
    with pytest.raises(ValueError, match="does not declare"):
        seeded_board(scenarios=["ocr-request-only"], token_budgets={"ocr-request-only": 100})
