"""进程内 Feature Flag 服务：规则求值、渐进放量与影响面回滚。

不实现网络、持久化、重启恢复、数据库、并发控制或第三方依赖。
"""

from __future__ import annotations

import copy
import hashlib

from .errors import (
    FlagNotFoundError,
    InvalidDefinitionError,
    MissingSubjectError,
    RevisionConflictError,
    RevisionNotFoundError,
    RollbackConflictError,
)

OPERATORS = ("equals", "in", "greater_than")

_REASON_DISABLED = "disabled"
_REASON_RULE = "rule"
_REASON_ROLLOUT = "rollout"
_REASON_DEFAULT = "default"


def _bucket(flag_key: str, salt: str, subject_id: str) -> int:
    """SHA-256(flag_key + ":" + salt + ":" + subject_id) 前 8 字节大端整数对 10000 取余。"""
    digest = hashlib.sha256(
        ("{}:{}:{}".format(flag_key, salt, subject_id)).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10000


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_rule(rule) -> dict:
    if not isinstance(rule, dict):
        raise InvalidDefinitionError("rule 必须是 dict")
    attribute = rule.get("attribute")
    if not isinstance(attribute, str) or not attribute:
        raise InvalidDefinitionError("rule.attribute 必须是非空字符串")
    operator = rule.get("operator")
    if operator not in OPERATORS:
        raise InvalidDefinitionError(
            "rule.operator 仅支持 equals、in、greater_than"
        )
    if "value" not in rule:
        raise InvalidDefinitionError("rule 缺少 value")
    value = rule["value"]
    if operator == "in" and not isinstance(value, (list, tuple, set, frozenset)):
        raise InvalidDefinitionError("operator 为 in 时 value 必须是集合类")
    if operator == "greater_than" and not _is_number(value):
        raise InvalidDefinitionError("operator 为 greater_than 时 value 必须是数值")
    if "serve" not in rule:
        raise InvalidDefinitionError("rule 缺少 serve")
    return {
        "attribute": attribute,
        "operator": operator,
        "value": copy.deepcopy(value),
        "serve": copy.deepcopy(rule["serve"]),
    }


def _validate_rollout(rollout) -> dict:
    if not isinstance(rollout, dict):
        raise InvalidDefinitionError("rollout 必须是 dict")
    percentage = rollout.get("percentage")
    if not _is_number(percentage) or not 0 <= percentage <= 100:
        raise InvalidDefinitionError("rollout.percentage 必须是 [0, 100] 内的数值")
    salt = rollout.get("salt")
    if not isinstance(salt, str):
        raise InvalidDefinitionError("rollout.salt 必须是字符串")
    if "serve" not in rollout:
        raise InvalidDefinitionError("rollout 缺少 serve")
    return {
        "percentage": percentage,
        "salt": salt,
        "serve": copy.deepcopy(rollout["serve"]),
    }


def _validate_definition(definition) -> dict:
    if not isinstance(definition, dict):
        raise InvalidDefinitionError("definition 必须是 dict")
    for key in ("enabled", "default", "rules", "rollout"):
        if key not in definition:
            raise InvalidDefinitionError("definition 缺少字段: {}".format(key))
    enabled = definition["enabled"]
    if not isinstance(enabled, bool):
        raise InvalidDefinitionError("enabled 必须是 bool")
    rules = definition["rules"]
    if not isinstance(rules, (list, tuple)):
        raise InvalidDefinitionError("rules 必须是列表")
    return {
        "enabled": enabled,
        "default": copy.deepcopy(definition["default"]),
        "rules": [_validate_rule(rule) for rule in rules],
        "rollout": _validate_rollout(definition["rollout"]),
    }


def _rule_matches(rule: dict, context: dict) -> bool:
    attribute = rule["attribute"]
    if attribute not in context:
        return False
    actual = context[attribute]
    expected = rule["value"]
    operator = rule["operator"]
    if operator == "equals":
        return actual == expected
    if operator == "in":
        try:
            return actual in expected
        except TypeError:
            return False
    # greater_than
    try:
        return actual > expected
    except TypeError:
        return False


class FeatureFlagService:
    """进程内 Feature Flag 服务。

    - publish：保存 flag_key 的正整数 revision 及其 definition，新版本成为当前版本。
    - evaluate：对 context 求值，返回 enabled、reason、revision、bucket。
    - rollback：校验影响面后把当前版本切回目标 revision。
    """

    def __init__(self) -> None:
        # flag_key -> {"revisions": {revision: definition}, "current": revision}
        self._flags: dict = {}

    def publish(self, flag_key: str, revision: int, definition: dict) -> int:
        """发布新版本。配置无效抛 InvalidDefinitionError，重复 revision 抛 RevisionConflictError。"""
        if not isinstance(flag_key, str) or not flag_key:
            raise InvalidDefinitionError("flag_key 必须是非空字符串")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise InvalidDefinitionError("revision 必须是正整数")
        normalized = _validate_definition(definition)
        flag = self._flags.setdefault(flag_key, {"revisions": {}, "current": None})
        if revision in flag["revisions"]:
            raise RevisionConflictError(
                "flag {!r} 的 revision {} 已存在".format(flag_key, revision)
            )
        flag["revisions"][revision] = normalized
        flag["current"] = revision
        return revision

    def evaluate(self, flag_key: str, context: dict, revision: int | None = None) -> dict:
        """求值。返回 {"enabled", "reason", "revision", "bucket"}。

        reason 为 disabled、rule、rollout、default 之一。
        """
        flag = self._flags.get(flag_key)
        if flag is None:
            raise FlagNotFoundError("flag {!r} 不存在".format(flag_key))
        if revision is None:
            revision = flag["current"]
        elif revision not in flag["revisions"]:
            raise RevisionNotFoundError(
                "flag {!r} 的 revision {} 不存在".format(flag_key, revision)
            )
        definition = flag["revisions"][revision]
        context = context or {}

        for rule in definition["rules"]:
            if _rule_matches(rule, context):
                return self._result(rule["serve"], _REASON_RULE, revision, None)

        if not definition["enabled"]:
            return self._result(definition["default"], _REASON_DISABLED, revision, None)

        subject_id = context.get("subject_id")
        if subject_id is None or (isinstance(subject_id, str) and subject_id == ""):
            raise MissingSubjectError("放量求值需要非空的 subject_id")
        rollout = definition["rollout"]
        bucket = _bucket(flag_key, rollout["salt"], str(subject_id))
        if bucket < rollout["percentage"] * 100:
            return self._result(rollout["serve"], _REASON_ROLLOUT, revision, bucket)
        return self._result(definition["default"], _REASON_DEFAULT, revision, bucket)

    def rollback(
        self,
        flag_key: str,
        revision: int,
        subjects,
        expected_impacted,
    ) -> list:
        """把当前版本切回目标 revision。

        对 subjects 逐个比较当前版本与目标版本的求值结果，实际受影响集合与
        expected_impacted 不一致时抛 RollbackConflictError 且当前版本不变；
        一致则激活目标 revision 并返回受影响主体列表。
        """
        flag = self._flags.get(flag_key)
        if flag is None:
            raise FlagNotFoundError("flag {!r} 不存在".format(flag_key))
        if revision not in flag["revisions"]:
            raise RevisionNotFoundError(
                "flag {!r} 的 revision {} 不存在".format(flag_key, revision)
            )
        current = flag["current"]
        impacted = []
        for subject in subjects:
            context = {"subject_id": subject}
            before = self.evaluate(flag_key, context, current)
            after = self.evaluate(flag_key, context, revision)
            if before["enabled"] != after["enabled"]:
                impacted.append(subject)
        if set(impacted) != set(expected_impacted):
            raise RollbackConflictError(
                "回滚影响面不一致：实际 {!r}，预期 {!r}".format(
                    sorted(map(repr, impacted)), sorted(map(repr, expected_impacted))
                )
            )
        flag["current"] = revision
        return impacted

    @staticmethod
    def _result(enabled, reason: str, revision: int, bucket) -> dict:
        return {
            "enabled": copy.deepcopy(enabled),
            "reason": reason,
            "revision": revision,
            "bucket": bucket,
        }
