"""flag_rollout 模块的异常类型。"""


class FlagRolloutError(Exception):
    """flag_rollout 所有异常的基类。"""


class InvalidDefinitionError(FlagRolloutError):
    """publish 收到的 flag 配置（含 revision）无效。"""


class RevisionConflictError(FlagRolloutError):
    """同一 flag_key 下 revision 已存在，版本不可修改。"""


class FlagNotFoundError(FlagRolloutError):
    """指定的 flag_key 尚未发布。"""


class RevisionNotFoundError(FlagRolloutError):
    """指定的 revision 在该 flag_key 下不存在。"""


class MissingSubjectError(FlagRolloutError):
    """放量求值需要非空的 subject_id，但 context 中缺失或为空。"""


class RollbackConflictError(FlagRolloutError):
    """回滚实际影响面与预期不一致，当前版本保持不变。"""


class InvalidRolloutChangeError(FlagRolloutError):
    """渐进放量晋升的参数（percentage、subjects、expected_impacted）无效。"""


class RolloutConflictError(FlagRolloutError):
    """放量晋升实际影响面与预期不一致，不创建候选版本。"""
