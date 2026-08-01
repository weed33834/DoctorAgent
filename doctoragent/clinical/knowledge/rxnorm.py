"""RxNorm knowledge-source client (drug-name normalization + classification).

Docs: https://www.nlm.nih.gov/research/umls/rxnorm/index.html
REST API: https://rxnorm.nlm.nih.gov/APIs/

No API key required. The REST API defaults to XML; we request JSON by
appending ``.json`` to each resource path (the underlying resource is the
same — only the representation differs).
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


class RxNormClient:
    """Async client for the NLM RxNorm REST API."""

    BASE_URL = "https://rxnorm.nlm.nih.gov"

    def __init__(
        self,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = self.BASE_URL
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "doctoragent-clinical/0.1"},
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
        """GET ``path`` and return parsed JSON. ``None`` on 404."""
        response = await self.client.get(path, params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def normalize_drug_name(self, name: str) -> str | None:
        """Return the best-guess RxCUI for *name*, or ``None`` if not found."""
        data = await self._get_json(
            "/REST/approximateTerm.json",
            params={"term": name, "maxEntries": 1},
        )
        if not data:
            return None
        candidates = data.get("approximateGroup", {}).get("candidate", []) or []
        if not candidates:
            return None
        rxcui = candidates[0].get("rxcui")
        # RxNorm sometimes returns an empty string when no rxcui is assigned.
        return rxcui or None

    async def get_drug_info(self, rxcui: str) -> dict:
        """Return consolidated drug information for an RxCUI.

        Shape: ``{rxcui, name, tty, ingredient_of, brand_names}``.
        """
        props_data = await self._get_json(f"/REST/rxcui/{rxcui}/properties.json")
        props = (props_data or {}).get("properties", {}) or {}

        brand_data = await self._get_json(f"/REST/rxcui/{rxcui}/brands.json")
        brand_names: list[str] = []
        if brand_data:
            brand_groups = brand_data.get("brandGroup", {}) or {}
            brand_list = brand_groups.get("brand", [])
            if isinstance(brand_list, list):
                brand_names = [b.get("name", "") for b in brand_list if b.get("name")]
            elif isinstance(brand_list, dict):
                name = brand_list.get("name")
                if name:
                    brand_names = [name]

        related_data = await self._get_json(f"/REST/rxcui/{rxcui}/allrelated.json")
        ingredient_of: list[str] = []
        if related_data:
            for group in related_data.get("allRelatedGroup", {}).get("conceptGroup", []) or []:
                if group.get("tty") in ("SCD", "SBD"):
                    for concept in group.get("conceptProperties", []) or []:
                        n = concept.get("name")
                        if n:
                            ingredient_of.append(n)

        return {
            "rxcui": rxcui,
            "name": props.get("name", ""),
            "tty": props.get("tty", ""),
            "ingredient_of": ingredient_of,
            "brand_names": brand_names,
        }

    async def get_related_drugs(self, rxcui: str, relation: str = "IN") -> list[dict]:
        """Return related drugs for the given relation TTY (IN/SCD/SBD/PIN...)."""
        data = await self._get_json(f"/REST/rxcui/{rxcui}/allrelated.json")
        results: list[dict] = []
        if not data:
            return results
        for group in data.get("allRelatedGroup", {}).get("conceptGroup", []) or []:
            if group.get("tty") != relation:
                continue
            for concept in group.get("conceptProperties", []) or []:
                results.append(
                    {
                        "rxcui": concept.get("rxcui", ""),
                        "name": concept.get("name", ""),
                        "tty": group.get("tty", ""),
                    }
                )
        return results

    async def get_drug_classes(self, rxcui: str) -> list[dict]:
        """Return RxClass drug classifications for an RxCUI."""
        data = await self._get_json(
            "/REST/rxclass/class/byRxcui.json",
            params={"rxcui": rxcui},
        )
        if not data:
            return []
        info_list = data.get("rxclassDrugInfoList", {}).get("rxclassDrugInfo", []) or []
        classes: list[dict] = []
        seen: set[str] = set()
        for info in info_list:
            concept = info.get("rxclassMinConceptItem", {}) or {}
            class_id = concept.get("classId", "")
            if not class_id or class_id in seen:
                continue
            seen.add(class_id)
            classes.append(
                {
                    "class_id": class_id,
                    "name": concept.get("className", ""),
                    "type": concept.get("classType", ""),
                }
            )
        return classes
