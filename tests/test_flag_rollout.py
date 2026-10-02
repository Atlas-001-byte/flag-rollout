import hashlib

import pytest

from flag_rollout import (
    FeatureFlagService,
    FlagNotFoundError,
    InvalidDefinitionError,
    MissingSubjectError,
    RevisionConflictError,
    RevisionNotFoundError,
    RollbackConflictError,
)


def make_definition(**overrides):
    definition = {
        "enabled": True,
        "default": False,
        "rules": [],
        "rollout": {"percentage": 0, "salt": "s1", "serve": True},
    }
    definition.update(overrides)
    return definition


def bucket_of(flag_key, salt, subject_id):
    digest = hashlib.sha256(
        "{}:{}:{}".format(flag_key, salt, subject_id).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10000


@pytest.fixture
def service():
    return FeatureFlagService()


# ---------- publish ----------


def test_publish_and_evaluate_default(service):
    service.publish("new-ui", 1, make_definition())
    result = service.evaluate("new-ui", {"subject_id": "u1"})
    assert result == {"enabled": False, "reason": "default", "revision": 1,
                      "bucket": bucket_of("new-ui", "s1", "u1")}


def test_publish_rejects_non_positive_revision(service):
    for bad in (0, -1, 1.5, "2", True, None):
        with pytest.raises(InvalidDefinitionError):
            service.publish("f", bad, make_definition())


def test_publish_rejects_invalid_definition(service):
    with pytest.raises(InvalidDefinitionError):
        service.publish("f", 1, {"enabled": True})  # 缺字段
    with pytest.raises(InvalidDefinitionError):
        service.publish("f", 1, make_definition(enabled="yes"))
    with pytest.raises(InvalidDefinitionError):
        service.publish("f", 1, make_definition(rules="not-a-list"))
    with pytest.raises(InvalidDefinitionError):
        service.publish("f", 1, make_definition(
            rules=[{"attribute": "age", "operator": "contains", "value": 1, "serve": True}]))
    with pytest.raises(InvalidDefinitionError):
        service.publish("f", 1, make_definition(
            rules=[{"attribute": "age", "operator": "in", "value": 3, "serve": True}]))
    with pytest.raises(InvalidDefinitionError):
        service.publish("f", 1, make_definition(
            rules=[{"attribute": "age", "operator": "greater_than", "value": "x", "serve": True}]))
    with pytest.raises(InvalidDefinitionError):
        service.publish("f", 1, make_definition(
            rollout={"percentage": 101, "salt": "s", "serve": True}))
    with pytest.raises(InvalidDefinitionError):
        service.publish("f", 1, make_definition(
            rollout={"percentage": -1, "salt": "s", "serve": True}))
    with pytest.raises(InvalidDefinitionError):
        service.publish("f", 1, make_definition(
            rollout={"percentage": 50, "salt": 7, "serve": True}))
    with pytest.raises(InvalidDefinitionError):
        service.publish("f", 1, make_definition(
            rollout={"percentage": 50, "salt": "s"}))


def test_publish_duplicate_revision_conflicts(service):
    service.publish("f", 1, make_definition())
    with pytest.raises(RevisionConflictError):
        service.publish("f", 1, make_definition(default=True))
    # 冲突不改变已发布版本
    assert service.evaluate("f", {"subject_id": "u"})["enabled"] is False


def test_publish_does_not_keep_caller_mutation(service):
    definition = make_definition()
    service.publish("f", 1, definition)
    definition["default"] = True
    definition["rollout"]["serve"] = "mutated"
    assert service.evaluate("f", {"subject_id": "u"})["enabled"] is False


# ---------- evaluate ----------


def test_evaluate_unknown_flag(service):
    with pytest.raises(FlagNotFoundError):
        service.evaluate("nope", {})


def test_evaluate_unknown_revision(service):
    service.publish("f", 1, make_definition())
    with pytest.raises(RevisionNotFoundError):
        service.evaluate("f", {}, revision=2)


def test_evaluate_disabled_flag_returns_default(service):
    service.publish("f", 1, make_definition(enabled=False, default="off",
                                            rollout={"percentage": 100, "salt": "s", "serve": "on"}))
    result = service.evaluate("f", {"subject_id": "u"})
    assert result["enabled"] == "off"
    assert result["reason"] == "disabled"
    assert result["bucket"] is None


def test_rules_match_in_order_first_hit_wins(service):
    service.publish("f", 1, make_definition(rules=[
        {"attribute": "plan", "operator": "equals", "value": "pro", "serve": "rule-1"},
        {"attribute": "plan", "operator": "equals", "value": "pro", "serve": "rule-2"},
    ]))
    assert service.evaluate("f", {"plan": "pro"})["enabled"] == "rule-1"
    assert service.evaluate("f", {"plan": "pro"})["reason"] == "rule"


def test_rule_operators(service):
    service.publish("f", 1, make_definition(rules=[
        {"attribute": "age", "operator": "greater_than", "value": 18, "serve": "adult"},
        {"attribute": "tier", "operator": "in", "value": ["gold", "platinum"], "serve": "vip"},
        {"attribute": "country", "operator": "equals", "value": "CN", "serve": "cn"},
    ]))
    assert service.evaluate("f", {"age": 20})["enabled"] == "adult"
    assert service.evaluate("f", {"age": 18, "subject_id": "u"})["reason"] == "default"  # 不大于 18
    assert service.evaluate("f", {"tier": "gold"})["enabled"] == "vip"
    assert service.evaluate("f", {"tier": "silver", "subject_id": "u"})["reason"] == "default"
    assert service.evaluate("f", {"country": "CN"})["enabled"] == "cn"
    assert service.evaluate("f", {"country": "US", "subject_id": "u"})["reason"] == "default"
    # 属性缺失或类型不可比较时不命中
    assert service.evaluate("f", {"subject_id": "u"})["reason"] == "default"
    assert service.evaluate("f", {"age": "twenty", "subject_id": "u"})["reason"] == "default"


def test_rules_take_precedence_over_disabled(service):
    service.publish("f", 1, make_definition(
        enabled=False, default="off",
        rules=[{"attribute": "beta", "operator": "equals", "value": True, "serve": "on"}],
        rollout={"percentage": 100, "salt": "s", "serve": "rollout-on"},
    ))
    assert service.evaluate("f", {"beta": True})["enabled"] == "on"
    assert service.evaluate("f", {"beta": False})["reason"] == "disabled"


def test_rollout_percentage_boundaries(service):
    service.publish("f0", 1, make_definition(rollout={"percentage": 0, "salt": "s", "serve": True}))
    assert service.evaluate("f0", {"subject_id": "u"})["reason"] == "default"

    service.publish("f100", 1, make_definition(rollout={"percentage": 100, "salt": "s", "serve": True}))
    result = service.evaluate("f100", {"subject_id": "u"})
    assert result["reason"] == "rollout"
    assert result["enabled"] is True


def test_rollout_bucket_is_deterministic_and_salted(service):
    service.publish("f", 1, make_definition(rollout={"percentage": 50, "salt": "a", "serve": True}))
    first = service.evaluate("f", {"subject_id": "u1"})
    second = service.evaluate("f", {"subject_id": "u1"})
    assert first == second
    assert first["bucket"] == bucket_of("f", "a", "u1")

    service.publish("f", 2, make_definition(rollout={"percentage": 50, "salt": "b", "serve": True}))
    # 不同 salt 的 bucket 一般不同（不强制断言不等，只验证计算方式）
    assert service.evaluate("f", {"subject_id": "u1"})["bucket"] == bucket_of("f", "b", "u1")


def test_rollout_partial_percentage_splits_subjects(service):
    service.publish("f", 1, make_definition(rollout={"percentage": 50, "salt": "s", "serve": True}))
    subjects = ["user-{}".format(i) for i in range(200)]
    results = {s: service.evaluate("f", {"subject_id": s}) for s in subjects}
    for s, result in results.items():
        expected = bucket_of("f", "s", s) < 50 * 100
        assert result["enabled"] is expected
        assert result["reason"] == ("rollout" if expected else "default")
    reasons = {r["reason"] for r in results.values()}
    assert reasons == {"rollout", "default"}


def test_evaluate_requires_subject_for_rollout(service):
    service.publish("f", 1, make_definition())
    with pytest.raises(MissingSubjectError):
        service.evaluate("f", {})
    with pytest.raises(MissingSubjectError):
        service.evaluate("f", {"subject_id": ""})
    with pytest.raises(MissingSubjectError):
        service.evaluate("f", {"subject_id": None})


def test_evaluate_uses_current_revision_by_default(service):
    service.publish("f", 1, make_definition(default="v1"))
    service.publish("f", 2, make_definition(default="v2"))
    assert service.evaluate("f", {"subject_id": "u"})["enabled"] == "v2"
    # 指定 revision 的结果固定
    assert service.evaluate("f", {"subject_id": "u"}, revision=1)["enabled"] == "v1"


# ---------- rollback ----------


def build_rollout_service(service):
    service.publish("f", 1, make_definition(
        default="old", rollout={"percentage": 0, "salt": "s", "serve": "new"}))
    service.publish("f", 2, make_definition(
        default="old", rollout={"percentage": 100, "salt": "s", "serve": "new"}))
    return service


def test_rollback_activates_target_revision(service):
    build_rollout_service(service)
    subjects = ["u1", "u2"]
    impacted = service.rollback("f", 1, subjects, expected_impacted=["u1", "u2"])
    assert impacted == ["u1", "u2"]
    assert service.evaluate("f", {"subject_id": "u1"})["revision"] == 1
    assert service.evaluate("f", {"subject_id": "u1"})["enabled"] == "old"


def test_rollback_detects_partial_impact(service):
    service.publish("f", 1, make_definition(default="off"))
    service.publish("f", 2, make_definition(default="off", rules=[
        {"attribute": "subject_id", "operator": "in", "value": ["vip"], "serve": "on"},
    ]))
    impacted = service.rollback("f", 1, ["vip", "plain"], expected_impacted=["vip"])
    assert impacted == ["vip"]


def test_rollback_conflict_keeps_current_revision(service):
    build_rollout_service(service)
    with pytest.raises(RollbackConflictError):
        service.rollback("f", 1, ["u1"], expected_impacted=[])
    # 当前版本未被修改
    assert service.evaluate("f", {"subject_id": "u1"})["revision"] == 2


def test_rollback_unknown_flag_or_revision(service):
    service.publish("f", 1, make_definition())
    with pytest.raises(FlagNotFoundError):
        service.rollback("nope", 1, [], [])
    with pytest.raises(RevisionNotFoundError):
        service.rollback("f", 99, [], [])


def test_rollback_result_of_pinned_revision_is_fixed(service):
    build_rollout_service(service)
    before = service.evaluate("f", {"subject_id": "u1"}, revision=2)
    service.rollback("f", 1, ["u1"], expected_impacted=["u1"])
    after = service.evaluate("f", {"subject_id": "u1"}, revision=2)
    assert before == after
