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
    """promote_rollout 参数无效：percentage 越界/类型错误，或主体不可迭代/不可哈希。"""


class RolloutConflictError(FlagRolloutError):
    """渐进放量晋升的实际影响面与预期不一致，不创建候选版本，当前版本保持不变。"""


class PreviewValidationError(FlagRolloutError):
    """preview_change 的输入或状态校验失败。

    携带唯一且确定的 error_code（响应体中的 errorCode）与 HTTP status。
    预演为只读操作，抛出时不产生任何状态变更。
    """

    def __init__(self, error_code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status = status
