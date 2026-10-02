"""flag_rollout 模块的异常类型。"""


class FlagRolloutError(Exception):
    """模块内所有异常的基类。"""


class InvalidDefinitionError(FlagRolloutError):
    """publish 的 definition 或 revision 不合法。"""


class RevisionConflictError(FlagRolloutError):
    """同一 flag_key 下 revision 已存在，版本不可修改。"""


class FlagNotFoundError(FlagRolloutError):
    """flag_key 尚未发布。"""


class RevisionNotFoundError(FlagRolloutError):
    """指定的 revision 不存在。"""


class MissingSubjectError(FlagRolloutError):
    """放量求值需要非空的 subject_id，但 context 未提供。"""


class RollbackConflictError(FlagRolloutError):
    """回滚实际影响面与预期不一致，当前版本保持不变。"""
