import pytest
from unittest.mock import Mock

from aviapars.adapters.base import build_search_result, ResultStatus, Offer, SearchResult

@pytest.mark.parametrize(
    "has_offers, blocked, partial, expected_status",
    [
        (True, True, True, ResultStatus.OK),
        (True, True, False, ResultStatus.OK),
        (True, False, True, ResultStatus.OK),
        (True, False, False, ResultStatus.OK),
        (False, True, True, ResultStatus.BLOCKED),
        (False, True, False, ResultStatus.BLOCKED),
        (False, False, True, ResultStatus.UPSTREAM_ERROR),
        (False, False, False, ResultStatus.EMPTY),
    ]
)
def test_build_search_result(has_offers, blocked, partial, expected_status):
    mock_offer = Mock(spec=Offer)
    offers = [mock_offer] if has_offers else []

    result = build_search_result(offers, blocked=blocked, partial=partial)

    assert result.status == expected_status
    assert result.offers == offers
    assert result.partial == partial
