"""进程内 Feature Flag 服务：版本管理、规则求值、渐进放量与影响面回滚。

不实现网络、持久化、重启恢复、数据库、并发控制或第三方依赖。
"""

from __future__ import annotations

import copy
import hashlib
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

from .errors import (
    PREVIEW_ERROR_CANDIDATE_EVALUATION,
    PREVIEW_ERROR_CONTEXT_EMPTY,
    PREVIEW_ERROR_CONTEXT_NOT_LIST,
    PREVIEW_ERROR_CONTEXT_NOT_OBJECT,
    PREVIEW_ERROR_FLAG_KEY_EMPTY,
    PREVIEW_ERROR_INVALID_PAYLOAD,
    PREVIEW_ERROR_STAGE_NOT_OBJECT,
    PREVIEW_ERROR_STAGE_NAME_DUPLICATE,
    PREVIEW_ERROR_STAGE_NAME_INVALID,
    PREVIEW_ERROR_STAGE_OVERLAP,
    PREVIEW_ERROR_STAGE_PERCENTAGE_INVALID,
    PREVIEW_ERROR_STAGE_PERCENTAGE_ORDER,
    PREVIEW_ERROR_STAGES_EMPTY,
    PREVIEW_ERROR_STAGES_NOT_LIST,
    PREVIEW_ERROR_SUBJECT_DUPLICATE,
    PREVIEW_ERROR_SUBJECT_MISSING,
    FlagNotFoundError,
    InvalidDefinitionError,
    InvalidRolloutChangeError,
    InvalidRolloutPlanError,
    MissingSubjectError,
    PreviewValidationError,
    RevisionConflictError,
    RevisionNotFoundError,
    RollbackConflictError,
    RolloutConflictError,
    RolloutPlanConflictError,
    RolloutPlanStateError,
)

OPERATORS = ("equals", "in", "greater_than")

REASON_DISABLED = "disabled"
REASON_RULE = "rule"
REASON_ROLLOUT = "rollout"
REASON_DEFAULT = "default"

_BUCKET_BASE = 10000


class _FlagState:
    """单个 flag_key 的全部版本与当前激活 revision。"""

    __slots__ = ("revisions", "current")

    def __init__(self) -> None:
        self.revisions: Dict[int, Dict[str, Any]] = {}
        self.current: Optional[int] = None


class FeatureFlagService:
    """进程内 Feature Flag 服务。

    - publish：保存 flag_key 某个正整数 revision 的 definition，并激活该版本。
    - evaluate：按规则 → enabled → 放量桶的顺序求值。
    - rollback：校验影响面后把当前版本切回目标 revision。
    """

    def __init__(self) -> None:
        self._flags: Dict[str, _FlagState] = {}
        # 每个 flag_key 至多一个放量计划；仅存内存，不持久化、不恢复。
        self._plans: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------
    def publish(self, flag_key: str, revision: int, definition: Mapping[str, Any]) -> int:
        """保存并激活 flag_key 的一个新版本，返回 revision。

        配置无效抛 InvalidDefinitionError；revision 已存在抛 RevisionConflictError。
        """
        if not isinstance(flag_key, str) or not flag_key:
            raise InvalidDefinitionError("flag_key 必须是非空字符串")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise InvalidDefinitionError("revision 必须是正整数")
        normalized = self._validate_definition(definition)

        state = self._flags.setdefault(flag_key, _FlagState())
        if revision in state.revisions:
            raise RevisionConflictError(
                "flag_key=%r 的 revision=%d 已存在，版本不可修改" % (flag_key, revision)
            )
        state.revisions[revision] = normalized
        state.current = revision
        return revision

    # ------------------------------------------------------------------
    # evaluate
    # ------------------------------------------------------------------
    def evaluate(
        self,
        flag_key: str,
        context: Optional[Mapping[str, Any]] = None,
        revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """对 context 求值，返回 {"enabled", "reason", "revision", "bucket"}。

        未指定 revision 时使用当前激活版本。未进入放量阶段时 bucket 为 None。
        """
        state = self._flags.get(flag_key)
        if state is None:
            raise FlagNotFoundError("flag_key=%r 尚未发布" % (flag_key,))
        if revision is None:
            revision = state.current
        elif revision not in state.revisions:
            raise RevisionNotFoundError(
                "flag_key=%r 不存在 revision=%r" % (flag_key, revision)
            )
        if context is None:
            context = {}
        if not isinstance(context, Mapping):
            raise TypeError("context 必须是 Mapping")
        return self._evaluate_definition(flag_key, revision, state.revisions[revision], context)

    # ------------------------------------------------------------------
    # rollback
    # ------------------------------------------------------------------
    def rollback(
        self,
        flag_key: str,
        revision: int,
        subjects: Iterable[Mapping[str, Any]],
        expected_impacted: Iterable[Any],
    ) -> List[Any]:
        """校验影响面后把 flag_key 的当前版本切换为 revision，返回受影响主体列表。

        逐个比较受检主体在当前版本与目标版本下的求值结果，结果不同的主体
        （按 subject_id 计）构成实际影响集合；与 expected_impacted 不一致时
        抛 RollbackConflictError 且当前版本保持不变。
        """
        state = self._flags.get(flag_key)
        if state is None:
            raise FlagNotFoundError("flag_key=%r 尚未发布" % (flag_key,))
        if revision not in state.revisions:
            raise RevisionNotFoundError(
                "flag_key=%r 不存在 revision=%r" % (flag_key, revision)
            )

        current_revision = state.current
        impacted: Set[Any] = set()
        for context in subjects:
            before = self._evaluate_definition(
                flag_key, current_revision, state.revisions[current_revision], context
            )
            after = self._evaluate_definition(
                flag_key, revision, state.revisions[revision], context
            )
            if before["enabled"] != after["enabled"]:
                impacted.add(context.get("subject_id"))

        if impacted != set(expected_impacted):
            raise RollbackConflictError(
                "flag_key=%r 回滚到 revision=%r 的实际影响面 %r 与预期 %r 不一致"
                % (flag_key, revision, sorted(impacted, key=str), sorted(expected_impacted, key=str))
            )

        state.current = revision
        return sorted(impacted, key=str)

    # ------------------------------------------------------------------
    # promote_rollout
    # ------------------------------------------------------------------
    def promote_rollout(
        self,
        flag_key: str,
        percentage: Any,
        subjects: Iterable[Mapping[str, Any]],
        expected_impacted: Iterable[Any],
    ) -> Dict[str, Any]:
        """提高 rollout.percentage，确认影响面后创建并激活可回滚的新版本。

        从当前 revision 复制 definition，仅修改 rollout.percentage，其余字段保持
        不变；新 revision 为已有最大值加一。逐个比较当前版本与候选版本下主体的
        求值结果，仅 enabled 变化计入影响（reason、bucket、revision 变化忽略），
        同一 subject_id 合并。实际影响面与 expected_impacted 不一致时抛
        RolloutConflictError，不创建候选版本，当前版本保持不变。
        """
        state = self._flags.get(flag_key)
        if state is None:
            raise FlagNotFoundError("flag_key=%r 尚未发布" % (flag_key,))

        if isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
            raise InvalidRolloutChangeError("percentage 必须是 [0, 100] 内的 int 或 float")
        if not 0 <= percentage <= 100:
            raise InvalidRolloutChangeError("percentage 必须是 [0, 100] 内的 int 或 float")

        try:
            subject_list = list(subjects)
        except TypeError:
            raise InvalidRolloutChangeError("subjects 必须可迭代")
        try:
            expected_set = set(expected_impacted)
        except TypeError:
            raise InvalidRolloutChangeError("expected_impacted 必须可迭代且成员可哈希")

        current_revision = state.current
        new_revision = max(state.revisions) + 1
        candidate = copy.deepcopy(state.revisions[current_revision])
        candidate["rollout"]["percentage"] = percentage

        impacted: Set[Any] = set()
        for context in subject_list:
            before = self._evaluate_definition(
                flag_key, current_revision, state.revisions[current_revision], context
            )
            after = self._evaluate_definition(flag_key, new_revision, candidate, context)
            if before["enabled"] != after["enabled"]:
                try:
                    impacted.add(context.get("subject_id"))
                except TypeError:
                    raise InvalidRolloutChangeError("subject_id 必须可哈希")

        if impacted != expected_set:
            raise RolloutConflictError(
                "flag_key=%r 放量到 percentage=%r 的实际影响面 %r 与预期 %r 不一致"
                % (flag_key, percentage,
                   sorted(impacted, key=str), sorted(expected_set, key=str)),
                sorted(impacted, key=str),
            )

        state.revisions[new_revision] = candidate
        state.current = new_revision
        return {"revision": new_revision, "impacted": sorted(impacted, key=str)}

    # ------------------------------------------------------------------
    # create_rollout_plan：登记多阶段放量计划（不改 revision、不预建版本）
    # ------------------------------------------------------------------
    def create_rollout_plan(self, flag_key: str, stages: Any) -> Dict[str, Any]:
        """为 flag_key 登记一个多阶段放量计划，返回计划快照。

        stages 必须非空；每项 name 为非空且唯一的字符串，percentage 为非布尔
        有限数，严格递增、高于当前 rollout.percentage 且不超过 100。登记本身
        不修改任何 revision、不预建候选版本；同 flag 存在未完成计划时抛
        RolloutPlanConflictError。计划只存内存。
        """
        state = self._flags.get(flag_key)
        if state is None:
            raise FlagNotFoundError("flag_key=%r 尚未发布" % (flag_key,))

        try:
            stage_list = list(stages)
        except TypeError:
            raise InvalidRolloutPlanError("stages 必须是非空列表")
        if not stage_list:
            raise InvalidRolloutPlanError("stages 不能为空")

        base_revision = state.current
        base_percentage = state.revisions[base_revision]["rollout"]["percentage"]

        normalized: List[Dict[str, Any]] = []
        seen_names: Set[str] = set()
        previous = base_percentage
        for index, stage in enumerate(stage_list):
            if not isinstance(stage, Mapping):
                raise InvalidRolloutPlanError("stages[%d] 必须是 Mapping" % index)
            name = stage.get("name")
            if not isinstance(name, str) or not name:
                raise InvalidRolloutPlanError("stages[%d].name 必须是非空字符串" % index)
            if name in seen_names:
                raise InvalidRolloutPlanError("阶段名称不可重复：%r" % (name,))
            seen_names.add(name)
            if "percentage" not in stage:
                raise InvalidRolloutPlanError("stages[%d] 缺少 percentage" % index)
            percentage = stage["percentage"]
            if (
                isinstance(percentage, bool)
                or not isinstance(percentage, (int, float))
                or not math.isfinite(percentage)
            ):
                raise InvalidRolloutPlanError(
                    "stages[%d].percentage 必须是非布尔有限数" % index
                )
            if percentage > 100:
                raise InvalidRolloutPlanError(
                    "stages[%d].percentage 不能超过 100" % index
                )
            if percentage <= previous:
                raise InvalidRolloutPlanError(
                    "stages[%d].percentage=%r 必须严格高于前一阶段/当前比例 %r"
                    % (index, percentage, previous)
                )
            previous = percentage
            normalized.append({"name": name, "percentage": percentage, "index": index})

        existing = self._plans.get(flag_key)
        if existing is not None and not existing["completed"]:
            raise RolloutPlanConflictError(
                "flag_key=%r 已存在未完成的放量计划" % (flag_key,)
            )

        self._plans[flag_key] = {
            "stages": normalized,
            "next_index": 0,
            "confirmed_revision": base_revision,
            "completed": False,
        }
        return {
            "flagKey": flag_key,
            "baseRevision": base_revision,
            "basePercentage": base_percentage,
            "stages": copy.deepcopy(normalized),
        }

    # ------------------------------------------------------------------
    # advance_rollout_plan：推进计划的下一阶段
    # ------------------------------------------------------------------
    def advance_rollout_plan(
        self,
        flag_key: str,
        subjects: Iterable[Mapping[str, Any]],
        expected_impacted: Iterable[Any],
    ) -> Dict[str, Any]:
        """把 flag_key 的放量计划推进到下一阶段，返回推进结果。

        以当前 revision 的 definition 为基准，仅把 rollout.percentage 改为下一
        阶段比例，按 promote_rollout 的求值顺序（规则 → enabled → 放量桶）逐个
        比较主体在基准与候选下的 enabled；变化的 subject_id 去重后构成实际影响
        集合。与 expected_impacted 一致才创建并激活下一未用正整数 revision 并
        推进计划；不一致抛 RolloutConflictError（携带排序后的影响列表），不建
        版本、不推进。无计划、计划已完成或当前 revision 偏离最近确认值时抛
        RolloutPlanStateError；任何错误都不改版本或计划。
        """
        state = self._flags.get(flag_key)
        if state is None:
            raise FlagNotFoundError("flag_key=%r 尚未发布" % (flag_key,))

        plan = self._plans.get(flag_key)
        if plan is None:
            raise RolloutPlanStateError("flag_key=%r 没有放量计划" % (flag_key,))
        if plan["completed"]:
            raise RolloutPlanStateError("flag_key=%r 的放量计划已完成" % (flag_key,))
        if state.current != plan["confirmed_revision"]:
            raise RolloutPlanStateError(
                "flag_key=%r 当前 revision=%r 偏离计划最近确认的 revision=%r"
                % (flag_key, state.current, plan["confirmed_revision"])
            )

        try:
            subject_list = list(subjects)
        except TypeError:
            raise InvalidRolloutChangeError("subjects 必须可迭代")
        try:
            expected_set = set(expected_impacted)
        except TypeError:
            raise InvalidRolloutChangeError("expected_impacted 必须可迭代且成员可哈希")

        stage = plan["stages"][plan["next_index"]]
        current_revision = state.current
        new_revision = max(state.revisions) + 1
        candidate = copy.deepcopy(state.revisions[current_revision])
        candidate["rollout"]["percentage"] = stage["percentage"]

        impacted: Set[Any] = set()
        for context in subject_list:
            before = self._evaluate_definition(
                flag_key, current_revision, state.revisions[current_revision], context
            )
            after = self._evaluate_definition(flag_key, new_revision, candidate, context)
            if before["enabled"] != after["enabled"]:
                try:
                    impacted.add(context.get("subject_id"))
                except TypeError:
                    raise InvalidRolloutChangeError("subject_id 必须可哈希")

        if impacted != expected_set:
            raise RolloutConflictError(
                "flag_key=%r 推进到阶段 %r（percentage=%r）的实际影响面 %r 与预期 %r 不一致"
                % (flag_key, stage["name"], stage["percentage"],
                   sorted(impacted, key=str), sorted(expected_set, key=str)),
                sorted(impacted, key=str),
            )

        state.revisions[new_revision] = candidate
        state.current = new_revision
        plan["next_index"] += 1
        plan["confirmed_revision"] = new_revision
        remaining = len(plan["stages"]) - plan["next_index"]
        completed = remaining == 0
        plan["completed"] = completed
        return {
            "revision": new_revision,
            "stage": dict(stage),
            "impacted": sorted(impacted, key=str),
            "completed": completed,
            "remaining": remaining,
        }

    # ------------------------------------------------------------------
    # preview_change：只读灰度预演（不改变任何线上状态）
    # ------------------------------------------------------------------
    def preview_change(
        self,
        request: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """对一批请求上下文预演候选配置与候选灰度阶段的求值差异。

        请求体（Mapping）：
            flagKey:    目标 flag 的非空 key，必须为已发布 flag。
            definition: 候选 flag 配置，结构与 publish 的 definition 完全一致。
            stages:     一个或多个候选灰度阶段，非空列表，每项至少含
                        percentage（[0,100] 数值），可含 name 与 salt。
            contexts:   非空上下文列表，每项为 Mapping 且携带非空 subjectKey
                        （映射到既有求值语义的 subject_id）；subjectKey 不可重复。

        纯只读：不创建发布记录、不推进阶段、不修改当前配置、不触发回滚。
        任何非法输入抛 PreviewValidationError（携带确定 error_code，HTTP 层
        映射为 422）；成功返回 200 响应体结构。
        """
        if not isinstance(request, Mapping):
            raise PreviewValidationError(
                PREVIEW_ERROR_INVALID_PAYLOAD, "请求体必须是 JSON 对象"
            )

        flag_key = request.get("flagKey")
        if not isinstance(flag_key, str) or not flag_key:
            raise PreviewValidationError(
                PREVIEW_ERROR_FLAG_KEY_EMPTY, "flagKey 必须是非空字符串"
            )

        state = self._flags.get(flag_key)
        if state is None:
            # 沿用现有公开入口对未知 flag 的统一错误语义（HTTP 层既定映射）。
            raise FlagNotFoundError("flagKey=%r 尚未发布" % (flag_key,))

        contexts = request.get("contexts")
        if not isinstance(contexts, list):
            raise PreviewValidationError(
                PREVIEW_ERROR_CONTEXT_NOT_LIST, "contexts 必须是数组"
            )
        if not contexts:
            raise PreviewValidationError(
                PREVIEW_ERROR_CONTEXT_EMPTY, "contexts 不能为空"
            )

        normalized_contexts: List[Dict[str, Any]] = []
        seen_subjects: Set[Any] = set()
        duplicates: List[Any] = []
        for index, context in enumerate(contexts):
            if not isinstance(context, Mapping):
                raise PreviewValidationError(
                    PREVIEW_ERROR_CONTEXT_NOT_OBJECT,
                    "contexts[%d] 必须是对象" % index,
                    {"index": index},
                )
            subject_key = context.get("subjectKey")
            if subject_key is None or subject_key == "":
                raise PreviewValidationError(
                    PREVIEW_ERROR_SUBJECT_MISSING,
                    "contexts[%d] 缺少非空 subjectKey" % index,
                    {"index": index},
                )
            try:
                if subject_key in seen_subjects:
                    duplicates.append(subject_key)
                seen_subjects.add(subject_key)
            except TypeError:
                # 非字符串等不可哈希 subjectKey：视为缺失处理，给出确定错误码。
                raise PreviewValidationError(
                    PREVIEW_ERROR_SUBJECT_MISSING,
                    "contexts[%d].subjectKey 必须是非空可哈希标量" % index,
                    {"index": index},
                )
            eval_context = dict(context)
            # 既有求值语义只识别 subject_id；subjectKey 是预演入口的确定键。
            eval_context["subject_id"] = subject_key
            normalized_contexts.append(
                {"subject_key": subject_key, "context": eval_context}
            )
        if duplicates:
            raise PreviewValidationError(
                PREVIEW_ERROR_SUBJECT_DUPLICATE,
                "subjectKey 不可重复",
                {"duplicates": sorted(duplicates, key=str)},
            )

        stages = request.get("stages")
        if not isinstance(stages, list):
            raise PreviewValidationError(
                PREVIEW_ERROR_STAGES_NOT_LIST, "stages 必须是数组"
            )
        if not stages:
            raise PreviewValidationError(
                PREVIEW_ERROR_STAGES_EMPTY, "stages 不能为空"
            )

        normalized_stages: List[Dict[str, Any]] = []
        seen_stage_names: Set[str] = set()
        candidate_salt = None
        candidate_definition = request.get("definition")
        try:
            validated_candidate = self._validate_definition(candidate_definition)
            candidate_salt = validated_candidate["rollout"]["salt"]
        except InvalidDefinitionError as exc:
            # 候选配置无法按既有语义求值（结构形状不合法）。
            raise PreviewValidationError(
                PREVIEW_ERROR_CANDIDATE_EVALUATION,
                "候选配置无法按既有语义求值：%s" % exc,
            )

        for index, stage in enumerate(stages):
            if not isinstance(stage, Mapping):
                raise PreviewValidationError(
                    PREVIEW_ERROR_STAGE_NOT_OBJECT,
                    "stages[%d] 必须是对象" % index,
                    {"index": index},
                )
            if "percentage" not in stage:
                raise PreviewValidationError(
                    PREVIEW_ERROR_STAGE_PERCENTAGE_INVALID,
                    "stages[%d] 缺少 percentage" % index,
                    {"index": index},
                )
            percentage = stage["percentage"]
            if (
                isinstance(percentage, bool)
                or not isinstance(percentage, (int, float))
                or not 0 <= percentage <= 100
            ):
                raise PreviewValidationError(
                    PREVIEW_ERROR_STAGE_PERCENTAGE_INVALID,
                    "stages[%d].percentage 必须是 [0, 100] 内的数值" % index,
                    {"index": index},
                )
            name = stage.get("name", "stage-%d" % index)
            if not isinstance(name, str) or not name:
                raise PreviewValidationError(
                    PREVIEW_ERROR_STAGE_NAME_INVALID,
                    "stages[%d].name 必须是非空字符串" % index,
                    {"index": index},
                )
            if name in seen_stage_names:
                raise PreviewValidationError(
                    PREVIEW_ERROR_STAGE_NAME_DUPLICATE,
                    "阶段名称不可重复：%r" % name,
                    {"name": name},
                )
            seen_stage_names.add(name)
            # 每个候选阶段是独立放量环：salt 由候选 rollout.salt 与阶段序号
            # 确定性派生，使阶段之间可互斥且预演结果完全可重复。
            salt = "%s#preview-stage-%d" % (candidate_salt, index)
            normalized_stages.append(
                {"name": name, "percentage": percentage, "salt": salt, "index": index}
            )

        for index in range(1, len(normalized_stages)):
            previous = normalized_stages[index - 1]["percentage"]
            current = normalized_stages[index]["percentage"]
            if current < previous:
                raise PreviewValidationError(
                    PREVIEW_ERROR_STAGE_PERCENTAGE_ORDER,
                    "阶段比例必须按非递减排列：stages[%d]=%r < stages[%d]=%r"
                    % (index, current, index - 1, previous),
                    {"index": index},
                )

        current_revision = state.current
        current_definition = state.revisions[current_revision]

        # 候选阶段配置：候选 definition 的副本，仅覆盖 rollout 的 salt 与
        # percentage；其余字段（enabled/default/rules）保持候选语义。
        stage_definitions = []
        for stage in normalized_stages:
            definition = copy.deepcopy(validated_candidate)
            definition["rollout"]["percentage"] = stage["percentage"]
            definition["rollout"]["salt"] = stage["salt"]
            stage_definitions.append(definition)

        # 未命中任何阶段时的候选回退求值：与候选配置同构，仅把 percentage 置 0，
        # 即“规则 → enabled → 放量桶 0% 永不命中 → default”，结论确定。
        zero_definition = copy.deepcopy(validated_candidate)
        zero_definition["rollout"]["percentage"] = 0

        def safe_evaluate(definition: Mapping[str, Any], context: Mapping[str, Any]) -> tuple:
            try:
                return self._evaluate_core(flag_key, definition, context)
            except MissingSubjectError as exc:
                raise PreviewValidationError(
                    PREVIEW_ERROR_CANDIDATE_EVALUATION,
                    "候选配置无法按既有语义求值：%s" % exc,
                )

        results: List[Dict[str, Any]] = []
        affected: Set[Any] = set()
        turned_on = 0
        turned_off = 0
        unchanged = 0
        stage_hits: Dict[str, Set[Any]] = {
            stage["name"]: set() for stage in normalized_stages
        }

        for entry in normalized_contexts:
            subject_key = entry["subject_key"]
            context = entry["context"]

            before_value, _before_reason, before_rule, _before_bucket = (
                self._evaluate_core(flag_key, current_definition, context)
            )

            matched_stage = None
            candidate_value = None
            candidate_reason = None
            candidate_rule = None
            candidate_bucket = None
            for stage, definition in zip(normalized_stages, stage_definitions):
                value, reason, rule_id, bucket = safe_evaluate(definition, context)
                if reason == REASON_ROLLOUT:
                    stage_hits[stage["name"]].add(subject_key)
                    if matched_stage is not None:
                        raise PreviewValidationError(
                            PREVIEW_ERROR_STAGE_OVERLAP,
                            "subjectKey=%r 在两个候选阶段同时命中" % (subject_key,),
                            {
                                "subjectKey": subject_key,
                                "stages": sorted(
                                    [matched_stage, stage["name"]], key=str
                                ),
                            },
                        )
                    matched_stage = stage["name"]
                    candidate_value, candidate_reason, candidate_rule = (
                        value,
                        reason,
                        rule_id,
                    )
                    candidate_bucket = bucket

            # 未命中任何候选阶段：回退到候选配置本身（percentage=0 的同构求值）。
            if matched_stage is None:
                candidate_value, candidate_reason, candidate_rule, candidate_bucket = (
                    safe_evaluate(zero_definition, context)
                )

            before_bool = bool(before_value)
            after_bool = bool(candidate_value)
            if not before_bool and after_bool:
                change_reason = "off_to_on"
                turned_on += 1
                affected.add(subject_key)
            elif before_bool and not after_bool:
                change_reason = "on_to_off"
                turned_off += 1
                affected.add(subject_key)
            else:
                change_reason = "unchanged"
                unchanged += 1

            results.append(
                {
                    "subjectKey": subject_key,
                    "before": before_bool,
                    "after": after_bool,
                    "beforeRuleId": before_rule,
                    "afterRuleId": candidate_rule,
                    "changeReason": change_reason,
                    "stage": matched_stage,
                }
            )

        return {
            "flagKey": flag_key,
            "currentRevision": current_revision,
            "results": results,
            "summary": {
                "total": len(results),
                "offToOn": turned_on,
                "onToOff": turned_off,
                "unchanged": unchanged,
                "stageHits": {
                    stage["name"]: len(stage_hits[stage["name"]])
                    for stage in normalized_stages
                },
                "affectedSubjects": sorted(affected, key=str),
            },
        }

    # ------------------------------------------------------------------
    # 内部：求值
    # ------------------------------------------------------------------
    def _evaluate_definition(
        self,
        flag_key: str,
        revision: int,
        definition: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Dict[str, Any]:
        value, reason, _rule_id, bucket = self._evaluate_core(flag_key, definition, context)
        return self._result(value, reason, revision, bucket)

    def _evaluate_core(
        self,
        flag_key: str,
        definition: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> tuple:
        """返回 (serve 值, reason, 命中规则标识或 None, bucket 或 None)。

        与既有求值顺序完全一致：规则按序匹配 → enabled → 放量桶 → default。
        规则标识取 rule 的 "id"，缺省为规则在列表中的 0 基序号；该字段仅供
        预演结果使用，不改变既有求值语义与 evaluate 的返回结构。
        """
        # 规则按序匹配，首个命中项取 serve。
        for index, rule in enumerate(definition["rules"]):
            if self._rule_matches(rule, context):
                rule_id = rule.get("id", index)
                return rule["serve"], REASON_RULE, rule_id, None

        # 未命中规则且未启用，取 default。
        if not definition["enabled"]:
            return definition["default"], REASON_DISABLED, None, None

        # 渐进放量：需要非空 subject_id。
        subject_id = context.get("subject_id")
        if subject_id is None or subject_id == "":
            raise MissingSubjectError(
                "flag_key=%r 放量求值需要非空 subject_id" % (flag_key,)
            )
        rollout = definition["rollout"]
        bucket = self._bucket(flag_key, rollout["salt"], subject_id)
        if bucket < rollout["percentage"] * 100:
            return rollout["serve"], REASON_ROLLOUT, None, bucket
        return definition["default"], REASON_DEFAULT, None, bucket

    @staticmethod
    def _result(value: Any, reason: str, revision: int, bucket: Optional[int]) -> Dict[str, Any]:
        return {
            "enabled": copy.deepcopy(value),
            "reason": reason,
            "revision": revision,
            "bucket": bucket,
        }

    @staticmethod
    def _rule_matches(rule: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
        attribute = rule["attribute"]
        if attribute not in context:
            return False
        actual = context[attribute]
        expected = rule["value"]
        operator = rule["operator"]
        if operator == "equals":
            return bool(actual == expected)
        if operator == "in":
            try:
                return actual in expected
            except TypeError:
                return False
        # greater_than
        try:
            return bool(actual > expected)
        except TypeError:
            return False

    @staticmethod
    def _bucket(flag_key: str, salt: str, subject_id: Any) -> int:
        key = "%s:%s:%s" % (flag_key, salt, subject_id)
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % _BUCKET_BASE

    # ------------------------------------------------------------------
    # 内部：配置校验（返回深拷贝后的规范化 definition）
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_definition(definition: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(definition, Mapping):
            raise InvalidDefinitionError("definition 必须是 Mapping")
        for key in ("enabled", "default", "rules", "rollout"):
            if key not in definition:
                raise InvalidDefinitionError("definition 缺少字段 %r" % (key,))

        enabled = definition["enabled"]
        if not isinstance(enabled, bool):
            raise InvalidDefinitionError("enabled 必须是 bool")

        rules = definition["rules"]
        if not isinstance(rules, (list, tuple)):
            raise InvalidDefinitionError("rules 必须是列表")
        for index, rule in enumerate(rules):
            FeatureFlagService._validate_rule(index, rule)

        rollout = definition["rollout"]
        if not isinstance(rollout, Mapping):
            raise InvalidDefinitionError("rollout 必须是 Mapping")
        for key in ("percentage", "salt", "serve"):
            if key not in rollout:
                raise InvalidDefinitionError("rollout 缺少字段 %r" % (key,))
        percentage = rollout["percentage"]
        if (
            isinstance(percentage, bool)
            or not isinstance(percentage, (int, float))
            or not 0 <= percentage <= 100
        ):
            raise InvalidDefinitionError("rollout.percentage 必须是 [0, 100] 内的数值")
        if not isinstance(rollout["salt"], str):
            raise InvalidDefinitionError("rollout.salt 必须是字符串")

        return copy.deepcopy(dict(definition))

    @staticmethod
    def _validate_rule(index: int, rule: Any) -> None:
        where = "rules[%d]" % index
        if not isinstance(rule, Mapping):
            raise InvalidDefinitionError("%s 必须是 Mapping" % where)
        for key in ("attribute", "operator", "value", "serve"):
            if key not in rule:
                raise InvalidDefinitionError("%s 缺少字段 %r" % (where, key))
        attribute = rule["attribute"]
        if not isinstance(attribute, str) or not attribute:
            raise InvalidDefinitionError("%s.attribute 必须是非空字符串" % where)
        operator = rule["operator"]
        if operator not in OPERATORS:
            raise InvalidDefinitionError(
                "%s.operator 必须是 %s 之一" % (where, "/".join(OPERATORS))
            )
        value = rule["value"]
        if operator == "in" and not isinstance(value, (list, tuple, set, frozenset)):
            raise InvalidDefinitionError("%s.value 在 operator=in 时必须是集合类" % where)
        if operator == "greater_than" and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise InvalidDefinitionError("%s.value 在 operator=greater_than 时必须是数值" % where)
