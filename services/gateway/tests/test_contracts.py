from pathlib import Path

from memecho_gateway.providers.mock import MockProvider
from memecho_gateway.contracts import validate_result


async def test_mock_provider_result_passes_contract():
    result = await MockProvider().analyze(
        {"title": "test"}, ["test.wav"], {"request_id": "req_test"}
    )
    errors = validate_result(result)
    assert errors == []


async def test_mock_provider_text_only_mode_acoustic_weight_flagged():
    result = await MockProvider().analyze(
        {"title": "test"}, [], {"request_id": "req_test"}
    )
    assert result["analysis_mode"] == "text_only"
    errors = validate_result(result)
    acoustic_errors = [e for e in errors if "acoustic_weight" in e]
    assert len(acoustic_errors) == 3


async def test_valid_text_only_result_passes_contract():
    result = await MockProvider().analyze(
        {"title": "test"}, [], {"request_id": "req_test"}
    )
    for point in result.get("vad_series", []):
        point["acoustic_weight"] = 0
    errors = validate_result(result)
    assert errors == []
