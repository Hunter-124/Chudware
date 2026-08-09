"""Objective visual-question benchmark for measuring viewpoint usefulness."""
from .analysis import (
    DependenceStats,
    EvaluationReport,
    TrialResult,
    evaluate_answerer,
    highest_aggregate_selections,
)
from .answerers import Answerer, BlindAnswerer, ComponentDegradedAnswerer, OracleAnswerer
from .binding import bind_cached_labels_for_controls, bind_pixel_corpus, make_instrument_benchmark
from .catalog import build_question_catalog, catalogue_counts
from .schema import (
    QUESTION_KINDS,
    BenchmarkItem,
    CandidateObservation,
    CandidateView,
    PublicQuestion,
    QuestionSpec,
)

__all__ = [
    "QUESTION_KINDS",
    "Answerer",
    "BenchmarkItem",
    "BlindAnswerer",
    "CandidateObservation",
    "CandidateView",
    "ComponentDegradedAnswerer",
    "DependenceStats",
    "EvaluationReport",
    "OracleAnswerer",
    "PublicQuestion",
    "QuestionSpec",
    "TrialResult",
    "bind_cached_labels_for_controls",
    "bind_pixel_corpus",
    "build_question_catalog",
    "catalogue_counts",
    "evaluate_answerer",
    "highest_aggregate_selections",
    "make_instrument_benchmark",
]
