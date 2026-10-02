"""进程内 Feature Flag 服务：规则求值、渐进放量与影响面回滚。"""

from .errors import (
    FlagNotFoundError,
    FlagRolloutError,
    InvalidDefinitionError,
    MissingSubjectError,
    RevisionConflictError,
    RevisionNotFoundError,
    RollbackConflictError,
)
from .service import FeatureFlagService

__all__ = [
    "FeatureFlagService",
    "FlagRolloutError",
    "InvalidDefinitionError",
    "RevisionConflictError",
    "FlagNotFoundError",
    "RevisionNotFoundError",
    "MissingSubjectError",
    "RollbackConflictError",
]
