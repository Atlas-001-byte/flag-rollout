"""flag_rollout 模块的异常类型。"""

import copy
from typing import Any


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


class InvalidRolloutPlanError(FlagRolloutError):
    """多阶段放量计划的阶段输入非法（stages 为空、name 缺失/重复、percentage 非法或未严格递增等）。"""


class RolloutPlanConflictError(FlagRolloutError):
    """同一 flag_key 已存在未完成的放量计划，不能重复登记。"""


class RolloutPlanStateError(FlagRolloutError):
    """放量计划推进时状态不合法：无计划、计划已完成，或当前 revision 偏离最近确认值。"""


# 预演（preview_change）的确定性错误码：HTTP 适配层统一映射为 422。
PREVIEW_ERROR_INVALID_PAYLOAD = "invalid_payload"
PREVIEW_ERROR_FLAG_KEY_EMPTY = "flag_key_empty"
PREVIEW_ERROR_CONTEXT_NOT_LIST = "contexts_not_list"
PREVIEW_ERROR_CONTEXT_EMPTY = "contexts_empty"
PREVIEW_ERROR_CONTEXT_NOT_OBJECT = "context_not_object"
PREVIEW_ERROR_SUBJECT_MISSING = "subject_key_missing"
PREVIEW_ERROR_SUBJECT_DUPLICATE = "subject_key_duplicate"
PREVIEW_ERROR_STAGES_NOT_LIST = "stages_not_list"
PREVIEW_ERROR_STAGES_EMPTY = "stages_empty"
PREVIEW_ERROR_STAGE_NOT_OBJECT = "stage_not_object"
PREVIEW_ERROR_STAGE_NAME_INVALID = "stage_name_invalid"
PREVIEW_ERROR_STAGE_NAME_DUPLICATE = "stage_name_duplicate"
PREVIEW_ERROR_STAGE_PERCENTAGE_INVALID = "stage_percentage_invalid"
PREVIEW_ERROR_STAGE_PERCENTAGE_ORDER = "stage_percentages_not_non_decreasing"
PREVIEW_ERROR_STAGE_OVERLAP = "stage_overlap"
PREVIEW_ERROR_CANDIDATE_EVALUATION = "candidate_not_evaluable"


class PreviewValidationError(FlagRolloutError):
    """预演请求不合法（对应 HTTP 422）。

    ``error_code`` 是唯一且确定的机器可读错误码，``details`` 携带确定顺序的
    定位信息（如重复 subject_key、重叠阶段名）；不携带求值过程中的任何随机数据。
    """

    def __init__(self, error_code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = copy.deepcopy(details)
