from memecho_gateway.contracts import validate_result
from memecho_gateway.providers.mock import MockProvider


async def test_mock_provider_result_passes_contract():
    result = await MockProvider().analyze(
        {"title": "test"}, ["test.wav"], {"request_id": "req_test"}
    )
    assert validate_result(result) == []

