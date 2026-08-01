"""PubMed (NCBI E-utilities) knowledge-source client.

Docs: https://www.ncbi.nlm.nih.gov/books/NBK25501/

An optional API key raises rate limits from 3 req/s to 10 req/s:
  https://www.ncbi.nlm.nih.gov/books/NBK25497/#chapter2.Usage_Guidelines_and_Requiremen

Endpoints used:
  * ``esearch.fcgi`` — PMIDs matching a query (JSON).
  * ``esummary.fcgi`` — title / authors / journal / pubdate metadata (JSON).
  * ``efetch.fcgi`` — full abstract (XML; parsed for ``<AbstractText>``).
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# PubMed clinical-query filters (mirrors the NCBI Clinical Queries tool).
_CLINICAL_FILTERS = {
    "clinical_trial": "(Clinical Trial[ptyp] OR Randomized Controlled Trial[ptyp])",
    "review": "(Systematic Review[ptyp] OR Meta-Analysis[ptyp] OR Review[ptyp])",
    "guideline": "(Practice Guideline[ptyp] OR Guideline[Title])",
}


class PubMedClient:
    """Async client for the NCBI E-utilities (PubMed) API."""

    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = self.BASE_URL
        # 10 req/s with key, 3 req/s without (informational only).
        self.rate_limit_per_second = 10 if api_key else 3
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
    async def _get_text(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> str | None:
        """GET ``path`` and return the response body. ``None`` on 404."""
        response = await self.client.get(path, params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET ``path`` and return parsed JSON. ``None`` on 404."""
        text = await self._get_text(path, params=params)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("PubMed returned non-JSON body from %s", path)
            return None

    async def search(
        self,
        query: str,
        max_results: int = 10,
        sort: str = "relevance",
    ) -> list[dict]:
        """Search PubMed and return summary metadata for each PMID.

        Each item shape: ``{pmid, title, abstract, authors, journal, pubdate}``.
        The ``abstract`` field is empty here — call :meth:`get_abstract` to
        fetch the full text for a specific PMID (avoids N+1 efetch calls).
        """
        search_data = await self._get_json(
            "/esearch.fcgi",
            params={
                "db": "pubmed",
                "term": query,
                "retmax": max_results,
                "retmode": "json",
                "sort": sort,
            },
        )
        if not search_data:
            return []
        pmids = search_data.get("esearchresult", {}).get("idlist", []) or []
        if not pmids:
            return []

        summary_data = await self._get_json(
            "/esummary.fcgi",
            params={"db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
        )
        result_obj = (summary_data or {}).get("result", {}) or {}

        articles: list[dict] = []
        for pmid in pmids:
            entry = result_obj.get(pmid, {}) or {}
            if not entry or "error" in entry:
                articles.append(
                    {
                        "pmid": pmid,
                        "title": "",
                        "abstract": "",
                        "authors": [],
                        "journal": "",
                        "pubdate": "",
                    }
                )
                continue
            authors = [a.get("name", "") for a in entry.get("authors", []) or [] if a.get("name")]
            articles.append(
                {
                    "pmid": pmid,
                    "title": entry.get("title", ""),
                    "abstract": "",
                    "authors": authors,
                    "journal": entry.get("source", ""),
                    "pubdate": entry.get("pubdate", ""),
                }
            )
        return articles

    async def get_abstract(self, pmid: str) -> str | None:
        """Fetch the full abstract for a single PMID via ``efetch.fcgi``.

        Returns ``None`` when the PMID is unknown or has no abstract.
        """
        text = await self._get_text(
            "/efetch.fcgi",
            params={
                "db": "pubmed",
                "id": pmid,
                "retmode": "xml",
                "rettype": "abstract",
            },
        )
        if not text:
            return None
        try:
            root = ET.fromstring(text)  # nosec B314
        except ET.ParseError:
            logger.warning("Failed to parse PubMed XML for PMID %s", pmid)
            return None
        for article in root.iter("PubmedArticle"):
            parts = [elem.text or "" for elem in article.iter("AbstractText")]
            if parts:
                return " ".join(part.strip() for part in parts).strip()
        return None

    async def search_clinical(self, query: str, max_results: int = 5) -> list[dict]:
        """Search PubMed restricted to clinical trials / reviews / guidelines."""
        filter_clause = " OR ".join(_CLINICAL_FILTERS.values())
        combined = f"({query}) AND ({filter_clause})"
        return await self.search(combined, max_results=max_results, sort="relevance")
