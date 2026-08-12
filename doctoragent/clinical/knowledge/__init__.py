"""Medical knowledge-source clients (openFDA / RxNorm / PubMed) and the
deterministic drug-interaction engine.

All clients are ``httpx.AsyncClient``-based and decorated with tenacity retry.
They gracefully return empty results on 404 instead of raising, so the
clinical agent layer can treat "no data" and "error" uniformly.
"""

from doctoragent.clinical.knowledge.drug_interactions import (
    LOCAL_DDI_KNOWLEDGE,
    DrugInteractionResult,
    check_drug_interactions,
    check_local_ddi,
    get_severity_rank,
)
from doctoragent.clinical.knowledge.openfda import OpenFDAClient
from doctoragent.clinical.knowledge.pubmed import PubMedClient
from doctoragent.clinical.knowledge.rxnorm import RxNormClient
from doctoragent.clinical.knowledge.seed import (
    KNOWLEDGE_DOCS,
    list_knowledge,
    seed_knowledge,
)

__all__ = [
    "DrugInteractionResult",
    "KNOWLEDGE_DOCS",
    "LOCAL_DDI_KNOWLEDGE",
    "OpenFDAClient",
    "PubMedClient",
    "RxNormClient",
    "check_drug_interactions",
    "check_local_ddi",
    "get_severity_rank",
    "list_knowledge",
    "seed_knowledge",
]
