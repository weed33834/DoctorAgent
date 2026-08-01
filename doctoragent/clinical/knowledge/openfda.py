"""openFDA knowledge-source client (drug labels + adverse events).

Docs: https://open.fda.gov/apis/

Rate limits (informational; callers integrate with ``api.rate_limit``):
  * without an API key: 40 requests / minute
  * with an API key:    240 requests / minute

openFDA signals "no matches found" with HTTP 404. We treat that as an empty
result (``[]`` / ``{}`` / ``""``) rather than an error so downstream clinical
logic never has to distinguish "no data" from "service failure".
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


class OpenFDAClient:
    """Async client for the openFDA drug label & adverse-event endpoints."""

    BASE_URL = "https://api.fda.gov"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = self.BASE_URL
        # 40 req/min without key, 240 req/min with key (informational only).
        self.rate_limit_per_minute = 240 if api_key else 40
        params: dict[str, str] = {}
        if api_key:
            params["api_key"] = api_key
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "doctoragent-clinical/0.1"},
            params=params,
            follow_redirects=True,
            transport=transport,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self.client.aclose()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
        wait=wait_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET ``path`` and return parsed JSON. ``None`` on openFDA's 404 sentinel."""
        response = await self.client.get(path, params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def search_drug_label(self, drug_name: str, limit: int = 5) -> list[dict]:
        """Search drug labeling by generic name. Returns ``[]`` on no match."""
        data = await self._get_json(
            "/drug/label.json",
            params={"search": f'openfda.generic_name:"{drug_name}"', "limit": limit},
        )
        if data is None:
            return []
        return data.get("results", []) or []

    async def get_drug_label(self, spl_set_id: str) -> dict:
        """Fetch a complete drug label by its SPL Set ID. ``{}`` on no match."""
        data = await self._get_json(
            "/drug/label.json",
            params={"search": f'openfda.spl_set_id:"{spl_set_id}"', "limit": 1},
        )
        if data is None:
            return {}
        results = data.get("results", []) or []
        return results[0] if results else {}

    async def search_adverse_events(self, drug_name: str, limit: int = 10) -> list[dict]:
        """Search adverse-event reports for a drug. Returns ``[]`` on no match."""
        data = await self._get_json(
            "/drug/event.json",
            params={
                "search": f'patient.drug.openfda.generic_name:"{drug_name}"',
                "limit": limit,
            },
        )
        if data is None:
            return []
        return data.get("results", []) or []

    async def get_interactions_section(self, drug_name: str) -> str:
        """Extract the ``drug_interactions`` section text from the drug label.

        Returns an empty string when no label or no interactions section is
        found. The returned text is the raw label section — the
        :func:`check_drug_interactions` engine parses it for severity signals.
        """
        results = await self.search_drug_label(drug_name, limit=5)
        for result in results:
            section = result.get("drug_interactions")
            if isinstance(section, list) and section:
                return "\n".join(str(s) for s in section)
            if isinstance(section, str) and section:
                return section
        return ""
