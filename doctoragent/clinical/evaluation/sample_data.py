"""Tiny built-in clinical QA samples for offline benchmarking.

These are intentionally small (5 + 5 records) and hand-curated so the
benchmark suite is runnable with zero network access — every CI run and
demo can exercise the full scorer / report path. For real evaluation,
load the full MedQA / PubMedQA datasets via HuggingFace (see
:mod:`doctoragent.clinical.evaluation.datasets`) or a local JSONL file.

Records use the raw upstream schema; :func:`datasets._normalise_record`
maps them to the canonical shape.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SAMPLE_MEDQA", "SAMPLE_PUBMEDQA"]


# MedQA (USMLE Step-style) — 4-option MCQ. ``answer_idx`` is 0-based.
SAMPLE_MEDQA: list[dict[str, Any]] = [
    {
        "case_id": "medqa-001",
        "question": (
            "A 55-year-old man presents with crushing chest pain radiating to "
            "the left arm, diaphoresis, and dyspnea. ECG shows ST-segment "
            "elevation in leads II, III, and aVF. Which coronary artery is "
            "most likely occluded?"
        ),
        "options": {
            "A": "Left anterior descending",
            "B": "Right coronary artery",
            "C": "Left circumflex",
            "D": "Obtuse marginal",
        },
        "answer_idx": "B",
        "rationale": (
            "Inferior STEMI (II/III/aVF) is most commonly caused by occlusion "
            "of the right coronary artery."
        ),
    },
    {
        "case_id": "medqa-002",
        "question": (
            "A 28-year-old woman has fatigue, cold intolerance, and "
            "constipation. Labs: TSH 18 mIU/L (high), free T4 low. Which is "
            "the most likely diagnosis?"
        ),
        "options": {
            "A": "Primary hyperthyroidism",
            "B": "Secondary hypothyroidism",
            "C": "Primary hypothyroidism",
            "D": "Subclinical hyperthyroidism",
        },
        "answer_idx": "C",
        "rationale": ("High TSH + low free T4 = primary hypothyroidism (thyroid gland failure)."),
    },
    {
        "case_id": "medqa-003",
        "question": (
            "A 60-year-old man with type 2 diabetes has HbA1c 9.5% despite "
            "metformin and lifestyle. eGFR is 45. Which medication class is "
            "preferred to add for cardiovascular benefit?"
        ),
        "options": {
            "A": "Sulfonylureas",
            "B": "SGLT2 inhibitors",
            "C": "Thiazolidinediones",
            "D": "Meglitinides",
        },
        "answer_idx": "B",
        "rationale": (
            "SGLT2 inhibitors reduce CV events and slow CKD progression; "
            "preferred when established ASCVD/CKD."
        ),
    },
    {
        "case_id": "medqa-004",
        "question": (
            "A 3-year-old child has recurrent bacterial infections, low "
            "serum IgG/IgA, normal IgM, and defective CD40 ligand on T "
            "cells. Which is the diagnosis?"
        ),
        "options": {
            "A": "X-linked agammaglobulinemia",
            "B": "Common variable immunodeficiency",
            "C": "Hyper-IgM syndrome",
            "D": "Severe combined immunodeficiency",
        },
        "answer_idx": "C",
        "rationale": ("Defective CD40L → class-switch failure → high IgM, low IgG/IgA."),
    },
    {
        "case_id": "medqa-005",
        "question": (
            "A 45-year-old woman has recurrent kidney stones. Labs show "
            "hypercalcemia, hypophosphatemia, high PTH. Which is the most "
            "likely cause?"
        ),
        "options": {
            "A": "Primary hyperparathyroidism",
            "B": "Vitamin D intoxication",
            "C": "Sarcoidosis",
            "D": "Milk-alkali syndrome",
        },
        "answer_idx": "A",
        "rationale": (
            "High PTH + hypercalcemia + hypophosphatemia = primary "
            "hyperparathyroidism (usually parathyroid adenoma)."
        ),
    },
]


# PubMedQA — yes/no/maybe over a research abstract + question.
SAMPLE_PUBMEDQA: list[dict[str, Any]] = [
    {
        "case_id": "pubmedqa-001",
        "pubid": "1",
        "question": "Does metformin reduce all-cause mortality in type 2 diabetes?",
        "context": {
            "contexts": [
                "Metformin is first-line therapy for type 2 diabetes.",
                "Long-term cohort studies show lower cardiovascular and "
                "all-cause mortality compared to sulfonylureas.",
            ],
            "labels": ["BACKGROUND", "RESULT"],
            "meshes": ["Metformin", "Diabetes Mellitus, Type 2"],
        },
        "long_answer": (
            "Yes — metformin is associated with reduced all-cause mortality in type 2 diabetes."
        ),
        "final_decision": "yes",
    },
    {
        "case_id": "pubmedqa-002",
        "pubid": "2",
        "question": "Is cranberry extract an effective treatment for UTI?",
        "context": {
            "contexts": [
                "Cranberry products reduce recurrent UTI in young healthy women.",
                "No benefit has been demonstrated for treating an established UTI.",
            ],
            "labels": ["BACKGROUND", "RESULT"],
            "meshes": ["Vaccinium macrocarpon", "Urinary Tract Infections"],
        },
        "long_answer": (
            "No — cranberry extract may help prevention but is not an effective "
            "treatment for active UTI."
        ),
        "final_decision": "no",
    },
    {
        "case_id": "pubmedqa-003",
        "pubid": "3",
        "question": "Does vitamin D supplementation prevent fractures in the elderly?",
        "context": {
            "contexts": [
                "Trials show mixed results for vitamin D alone.",
                "Combined vitamin D + calcium shows modest benefit in institutionalised elderly.",
            ],
            "labels": ["BACKGROUND", "RESULT"],
            "meshes": ["Vitamin D", "Fractures, Bone"],
        },
        "long_answer": (
            "Maybe — benefit is modest and mostly confined to combined "
            "vitamin D + calcium in institutionalised populations."
        ),
        "final_decision": "maybe",
    },
    {
        "case_id": "pubmedqa-004",
        "pubid": "4",
        "question": "Is nicotine replacement therapy effective for smoking cessation?",
        "context": {
            "contexts": [
                "NRT doubles quit rates vs placebo.",
                "Effect is consistent across patches, gum, and lozenges.",
            ],
            "labels": ["BACKGROUND", "RESULT"],
            "meshes": ["Nicotine", "Smoking Cessation"],
        },
        "long_answer": "Yes — NRT approximately doubles smoking cessation rates.",
        "final_decision": "yes",
    },
    {
        "case_id": "pubmedqa-005",
        "pubid": "5",
        "question": "Does beta-carotene supplementation reduce lung cancer risk in smokers?",
        "context": {
            "contexts": [
                "Two large RCTs found increased lung cancer risk with "
                "high-dose beta-carotene in smokers.",
            ],
            "labels": ["RESULT"],
            "meshes": ["beta-Carotene", "Lung Neoplasms"],
        },
        "long_answer": ("No — high-dose beta-carotene in smokers increases lung cancer risk."),
        "final_decision": "no",
    },
]
