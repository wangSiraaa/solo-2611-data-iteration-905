"""Independent elevation catalog and opened-outlet storage plan verifier.

Synthetic discrete model only -- not for field hydraulic decisions.
"""

from .catalog import Catalog
from .experiment import (
    MAX_OUTLETS,
    MAX_POINTS,
    MAX_SLICES,
    Experiment,
    ExperimentError,
    ExperimentStore,
    SurveyPoint,
    build_experiment,
)
from .model import (
    BatchError,
    BatchResult,
    Cell,
    Outlet,
    Slice,
    SourceRef,
    TileSnapshot,
    WaterResult,
    canonical_json,
    sha256_hex,
)
from .publisher import PublishError, PublishResult, Publisher, VerifyResult
from .solver import BottleneckCache, SolveResult, SolverStats, enumerate_subsets, solve
from .surveyor import (
    FeasibilityTable,
    PlanNode,
    PlanResult,
    SessionError,
    SessionStore,
    SurveyOrder,
    SurveySession,
    plan_survey,
)

__all__ = [
    "BatchError",
    "BatchResult",
    "BottleneckCache",
    "Catalog",
    "Cell",
    "Experiment",
    "ExperimentError",
    "ExperimentStore",
    "FeasibilityTable",
    "MAX_OUTLETS",
    "MAX_POINTS",
    "MAX_SLICES",
    "Outlet",
    "PlanNode",
    "PlanResult",
    "PublishError",
    "PublishResult",
    "Publisher",
    "SessionError",
    "SessionStore",
    "Slice",
    "SolveResult",
    "SolverStats",
    "SourceRef",
    "SurveyOrder",
    "SurveyPoint",
    "SurveySession",
    "TileSnapshot",
    "VerifyResult",
    "WaterResult",
    "build_experiment",
    "canonical_json",
    "enumerate_subsets",
    "plan_survey",
    "sha256_hex",
    "solve",
]
