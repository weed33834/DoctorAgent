"""Dataset loaders for the clinical benchmark suite.

Two real-world datasets are supported:

* **MedQA** (USMLE-style 4/5-option multiple choice) — answer is a letter.
* **PubMedQA** (yes / no / maybe over a research abstract) — answer is a
  closed-label long-answer.

Loaders resolve in this order:

1. A local JSONL/JSON file path passed explicitly (offline, deterministic).
2. The HuggingFace ``datasets`` library (``bigbio/medqa`` / ``qiaojin/PubMedQA``)
   when the optional ``datasets`` extra is installed.
3. The tiny built-in sample datasets shipped in
   :mod:`doctoragent.clinical.evaluation.sample_data` (always available).

The JSONL record schema is the ``DatasetShape`` contract — every loader,
whichever source it pulls from, normalises to it so the scorer is
source-agnostic.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from doctoragent.clinical.evaluation.sample_data import (
    SAMPLE_MEDQA,
    SAMPLE_PUBMEDQA,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DATASET_SHAPES",
    "DatasetShape",
    "load_dataset",
]


# ---------------------------------------------------------------------------
# Dataset shape contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatasetShape:
    """Canonical record shape every loader normalises to.

    ``answer`` is the *gold* answer — a letter for MCQ datasets
    (``"A"``/``"B"``…), a label for classification datasets
    (``"yes"``/``"no"``/``"maybe"``). ``answer_idx`` is the 0-based index
    into ``options`` when the dataset is MCQ; ``-1`` for non-MCQ.
    """

    name: str
    kind: str  # "mcq" | "classification" | "freeform"
    #: HF dataset id (best-effort; may move/redirect upstream).
    hf_id: str
    #: HF config / split name.
    hf_config: str | None
    hf_split: str
    #: Closed label set for classification datasets; ``()`` for MCQ/free-form.
    labels: tuple[str, ...]


DATASET_SHAPES: dict[str, DatasetShape] = {
    "medqa": DatasetShape(
        name="medqa",
        kind="mcq",
        # The most-cited MedQA mirror on the HF hub. BigBio also hosts a
        # variant (``bigbio/medqa``); the loader tries several ids.
        hf_id="bigbio/med_qa",
        hf_config="medqa",
        hf_split="test",
        labels=(),
    ),
    "pubmedqa": DatasetShape(
        name="pubmedqa",
        kind="classification",
        hf_id="qiaojin/PubMedQA",
        hf_config="pqa_artificial",
        hf_split="validation",
        labels=("yes", "no", "maybe"),
    ),
}


# ---------------------------------------------------------------------------
# JSONL normalisation
# ---------------------------------------------------------------------------


def _normalise_record(raw: dict[str, Any], shape: DatasetShape) -> dict[str, Any]:
    """Coerce an arbitrary dataset record into the canonical schema.

    Accepts the common key-spellings each dataset publishes (``question``,
    ``question_text``, ``query``; ``options`` as list or dict; ``answer``,
    ``answer_idx``, ``gold``; ``context``, ``abstract``, ``LONG_ANSWER``).
    """
    q = (
        raw.get("question")
        or raw.get("question_text")
        or raw.get("query")
        or raw.get("input")
        or ""
    )
    if isinstance(q, dict):  # PubMedQA nests {"question": ..., "context": ...}
        q = q.get("question") or ""

    # Options — accept list, dict, or None.
    options = raw.get("options")
    if isinstance(options, dict):
        # {"A": "...", "B": "..."} or {"0": "..."} — sort by key. Use a
        # separate variable so the except branch still sees the original
        # dict (mypy can't narrow through the reassignment).
        opts_map: dict[Any, Any] = options
        try:
            options = [opts_map[k] for k in sorted(opts_map)]
        except KeyError:
            options = list(opts_map.values())
    if not isinstance(options, list):
        options = []

    # Answer. PubMedQA publishes the label under ``final_decision`` (a
    # string in the upstream schema); some mirrors nest it as a dict.
    answer = (
        raw.get("answer")
        or raw.get("gold")
        or raw.get("answer_idx")
        or raw.get("final_decision")
        or ""
    )
    if isinstance(answer, dict):  # PubMedQA {"final_decision": "yes"}
        answer = answer.get("final_decision") or ""

    answer_idx = -1
    if shape.kind == "mcq":
        # Letter → index; bare integer → int.
        if isinstance(answer, str) and len(answer) == 1 and answer.isalpha():
            answer_idx = ord(answer.upper()) - ord("A")
        elif isinstance(answer, int):
            answer_idx = answer
        elif isinstance(answer, str) and answer.isdigit():
            answer_idx = int(answer)
        if answer_idx < 0 or answer_idx >= len(options):
            answer_idx = max(0, min(len(options) - 1, answer_idx)) if options else -1
        if isinstance(answer, int):
            answer = chr(ord("A") + answer_idx) if 0 <= answer_idx < 26 else str(answer)

    context = raw.get("context") if not isinstance(raw.get("context"), str) else raw.get("context")
    # PubMedQA context is {"contexts": [...], "labels": [...], "meshes": [...]}.
    if isinstance(context, dict):
        ctx_parts = context.get("contexts") or []
        if isinstance(ctx_parts, list):
            context = "\n".join(str(c) for c in ctx_parts)
        else:
            context = json.dumps(context, ensure_ascii=False)
    elif context is None:
        context = raw.get("abstract") or raw.get("LONG_ANSWER") or ""

    return {
        "case_id": str(raw.get("case_id") or raw.get("id") or raw.get("pubid") or ""),
        "question": str(q),
        "options": [str(o) for o in options],
        "answer": str(answer),
        "answer_idx": int(answer_idx),
        "context": str(context) if context else "",
        "rationale": str(raw.get("rationale") or raw.get("CoT") or raw.get("long_answer") or ""),
        "source": shape.name,
        "meta": {
            k: v
            for k, v in raw.items()
            if k
            not in {
                "question",
                "question_text",
                "query",
                "input",
                "options",
                "answer",
                "gold",
                "answer_idx",
                "context",
                "abstract",
                "LONG_ANSWER",
                "rationale",
                "CoT",
                "case_id",
                "id",
                "pubid",
            }
        },
    }


def _load_jsonl(path: str | Path) -> Iterable[dict[str, Any]]:
    """Yield records from a JSONL or JSON-array file (auto-detect by suffix)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, list):
            yield from data
        elif isinstance(data, dict):
            # Maybe {"data": [...]} or {"train": [...]}.
            for v in data.values():
                if isinstance(v, list):
                    yield from v
                    break
            else:
                yield data
        return
    # JSONL.
    for line in text.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_dataset(
    name: str,
    *,
    path: str | Path | None = None,
    limit: int | None = None,
    split: str | None = None,
) -> list[dict[str, Any]]:
    """Load a clinical QA dataset as a list of canonical records.

    Resolution order:

    1. ``path`` — an explicit local JSONL/JSON file (always wins, offline).
    2. HuggingFace ``datasets`` (when installed) — downloads the real
       MedQA / PubMedQA test split. Network required.
    3. Built-in :mod:`sample_data` — a tiny hand-curated subset, always
       available so tests and demos run with zero dependencies.

    ``limit`` truncates the result; ``split`` overrides the default HF
    split defined in :data:`DATASET_SHAPES`.
    """
    name = name.lower().strip()
    shape = DATASET_SHAPES.get(name)
    if shape is None:
        raise ValueError(f"unknown dataset {name!r}; supported: {sorted(DATASET_SHAPES)}")
    split = split or shape.hf_split

    records: list[dict[str, Any]] = []

    # 1. Explicit local file.
    if path is not None:
        logger.info("loading %s from local file %s", name, path)
        records = [_normalise_record(r, shape) for r in _load_jsonl(path)]
    else:
        # 2. HuggingFace datasets (best-effort).
        hf_records = _try_hf(shape, split)
        if hf_records is not None:
            logger.info("loaded %s from HuggingFace (%d records)", name, len(hf_records))
            records = hf_records
        else:
            # 3. Built-in sample data.
            logger.info(
                "datasets/HF unavailable or %s not found — using built-in sample",
                name,
            )
            sample = SAMPLE_MEDQA if name == "medqa" else SAMPLE_PUBMEDQA
            records = [_normalise_record(r, shape) for r in sample]

    if limit is not None and limit > 0:
        records = records[:limit]

    # Stable case ids when missing.
    for i, r in enumerate(records):
        if not r.get("case_id"):
            r["case_id"] = f"{name}-{i:04d}"
    return records


def _try_hf(shape: DatasetShape, split: str) -> list[dict[str, Any]] | None:
    """Attempt to load from HuggingFace ``datasets``. ``None`` on any failure."""
    try:
        from datasets import load_dataset as hf_load_dataset  # type: ignore[import-not-found]
    except ImportError:
        return None
    # Several candidate ids — the HF hub re-organises over time; try in order.
    candidate_ids: list[tuple[str, str | None]] = []
    if shape.name == "medqa":
        candidate_ids = [
            ("bigbio/med_qa", "medqa"),
            ("bigbio/medqa", "medqa"),
            ("GBaker/MedQA-USMLE-4-options", None),
        ]
    elif shape.name == "pubmedqa":
        candidate_ids = [
            ("qiaojin/PubMedQA", "pqa_artificial"),
            ("qiaojin/PubMedQA", "pqa_labeled"),
            ("bigbio/pubmed_qa", None),
        ]
    for ds_id, cfg in candidate_ids:
        try:
            if cfg is not None:
                ds = hf_load_dataset(ds_id, cfg, split=split, revision="main")  # nosec B615
            else:
                ds = hf_load_dataset(ds_id, split=split, revision="main")  # nosec B615
        except Exception as exc:  # noqa: BLE001 — hub is volatile
            logger.debug("HF load %s/%s failed: %s", ds_id, cfg, exc)
            continue
        return [_normalise_record(dict(r), shape) for r in ds]
    return None
