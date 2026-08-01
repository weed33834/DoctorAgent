"""Tests for inter-layer Pydantic schemas."""

import pytest
from pydantic import ValidationError

from doctoragent.api.schemas import SearchQuery


def test_search_query_default_top_k() -> None:
    """SearchQuery uses a sensible default top_k."""
    query = SearchQuery(query="find invoices")
    assert query.top_k == 5


@pytest.mark.parametrize("top_k", [1, 50, 100])
def test_search_query_top_k_within_range(top_k: int) -> None:
    """top_k values inside 1-100 are accepted."""
    query = SearchQuery(query="find invoices", top_k=top_k)
    assert query.top_k == top_k


@pytest.mark.parametrize("top_k", [0, -1, 101, 1000])
def test_search_query_top_k_out_of_range(top_k: int) -> None:
    """top_k values outside 1-100 are rejected."""
    with pytest.raises(ValidationError):
        SearchQuery(query="find invoices", top_k=top_k)
