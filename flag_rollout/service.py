"""进程内 Feature Flag 服务：版本管理、规则求值、渐进放量与影响面回滚。

不实现网络、持久化、重启恢复、数据库、并发控制或第三方依赖。
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

from .errors import (
    FlagNotFoundError,
    InvalidDefinitionError,
    InvalidRolloutChangeError,
    MissingSubjectError,
    RevisionConflictError,
    RevisionNotFoundError,
    RollbackConflictError,
    RolloutConflictError,
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
    # 内部：求值
    # ------------------------------------------------------------------
    def _evaluate_definition(
        self,
        flag_key: str,
        revision: int,
        definition: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Dict[str, Any]:
        # 规则按序匹配，首个命中项取 serve。
        for rule in definition["rules"]:
            if self._rule_matches(rule, context):
                return self._result(rule["serve"], REASON_RULE, revision, None)

        # 未命中规则且未启用，取 default。
        if not definition["enabled"]:
            return self._result(definition["default"], REASON_DISABLED, revision, None)

        # 渐进放量：需要非空 subject_id。
        subject_id = context.get("subject_id")
        if subject_id is None or subject_id == "":
            raise MissingSubjectError(
                "flag_key=%r 放量求值需要非空 subject_id" % (flag_key,)
            )
        rollout = definition["rollout"]
        bucket = self._bucket(flag_key, rollout["salt"], subject_id)
        if bucket < rollout["percentage"] * 100:
            return self._result(rollout["serve"], REASON_ROLLOUT, revision, bucket)
        return self._result(definition["default"], REASON_DEFAULT, revision, bucket)

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
