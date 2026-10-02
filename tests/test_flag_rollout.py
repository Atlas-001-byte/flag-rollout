"""flag_rollout 的单元测试，仅依赖标准库。"""

import hashlib
import unittest

from flag_rollout import (
    FeatureFlagService,
    FlagNotFoundError,
    InvalidDefinitionError,
    MissingSubjectError,
    RevisionConflictError,
    RevisionNotFoundError,
    RollbackConflictError,
)


def bucket_of(flag_key, salt, subject_id):
    digest = hashlib.sha256(("%s:%s:%s" % (flag_key, salt, subject_id)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 10000


def make_definition(**overrides):
    definition = {
        "enabled": True,
        "default": False,
        "rules": [],
        "rollout": {"percentage": 100, "salt": "s1", "serve": True},
    }
    definition.update(overrides)
    return definition


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()

    def test_publish_activates_revision(self):
        self.assertEqual(self.svc.publish("flag-a", 1, make_definition()), 1)
        result = self.svc.evaluate("flag-a", {"subject_id": "u1"})
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["reason"], "rollout")

    def test_duplicate_revision_conflicts(self):
        self.svc.publish("flag-a", 1, make_definition())
        with self.assertRaises(RevisionConflictError):
            self.svc.publish("flag-a", 1, make_definition())

    def test_invalid_revision(self):
        for bad in (0, -1, 1.5, "2", True, None):
            with self.assertRaises(InvalidDefinitionError, msg=repr(bad)):
                self.svc.publish("flag-a", bad, make_definition())

    def test_invalid_definition(self):
        bad_definitions = [
            None,
            {},
            {"enabled": True, "default": False, "rules": []},  # 缺 rollout
            make_definition(enabled=1),
            make_definition(rules={}),
            make_definition(rules=[{"attribute": "age", "operator": "equals", "value": 1}]),  # 缺 serve
            make_definition(rules=[{"attribute": "", "operator": "equals", "value": 1, "serve": 1}]),
            make_definition(rules=[{"attribute": "a", "operator": "contains", "value": 1, "serve": 1}]),
            make_definition(rules=[{"attribute": "a", "operator": "in", "value": 1, "serve": 1}]),
            make_definition(rules=[{"attribute": "a", "operator": "greater_than", "value": "x", "serve": 1}]),
            make_definition(rollout={"percentage": 101, "salt": "s", "serve": True}),
            make_definition(rollout={"percentage": -1, "salt": "s", "serve": True}),
            make_definition(rollout={"percentage": 50, "serve": True}),  # 缺 salt
            make_definition(rollout={"percentage": 50, "salt": 1, "serve": True}),
        ]
        for i, bad in enumerate(bad_definitions):
            with self.assertRaises(InvalidDefinitionError, msg="case %d" % i):
                self.svc.publish("flag-a", i + 1, bad)

    def test_definition_is_frozen_after_publish(self):
        definition = make_definition()
        self.svc.publish("flag-a", 1, definition)
        definition["rollout"]["percentage"] = 0
        definition["rules"].append({"attribute": "x", "operator": "equals", "value": 1, "serve": True})
        result = self.svc.evaluate("flag-a", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "rollout")
        self.assertTrue(result["enabled"])


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()

    def test_flag_not_found(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.evaluate("nope", {"subject_id": "u1"})

    def test_revision_not_found(self):
        self.svc.publish("flag-a", 1, make_definition())
        with self.assertRaises(RevisionNotFoundError):
            self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=2)

    def test_specified_revision_is_fixed(self):
        self.svc.publish("flag-a", 1, make_definition(rollout={"percentage": 100, "salt": "s", "serve": "v1"}))
        self.svc.publish("flag-a", 2, make_definition(rollout={"percentage": 100, "salt": "s", "serve": "v2"}))
        self.assertEqual(self.svc.evaluate("flag-a", {"subject_id": "u"})["enabled"], "v2")
        self.assertEqual(self.svc.evaluate("flag-a", {"subject_id": "u"}, revision=1)["enabled"], "v1")

    def test_disabled_flag_returns_default(self):
        self.svc.publish("flag-a", 1, make_definition(enabled=False, default="off"))
        result = self.svc.evaluate("flag-a", {"subject_id": "u1"})
        self.assertEqual(result, {"enabled": "off", "reason": "disabled", "revision": 1, "bucket": None})

    def test_rules_match_in_order_before_enabled_check(self):
        rules = [
            {"attribute": "country", "operator": "equals", "value": "CN", "serve": "cn"},
            {"attribute": "plan", "operator": "in", "value": ["pro", "team"], "serve": "paid"},
            {"attribute": "age", "operator": "greater_than", "value": 18, "serve": "adult"},
        ]
        self.svc.publish("flag-a", 1, make_definition(enabled=False, rules=rules))
        self.assertEqual(self.svc.evaluate("flag-a", {"country": "CN", "plan": "pro"})["enabled"], "cn")
        self.assertEqual(self.svc.evaluate("flag-a", {"plan": "team"})["enabled"], "paid")
        result = self.svc.evaluate("flag-a", {"age": 20})
        self.assertEqual((result["enabled"], result["reason"]), ("adult", "rule"))
        # 属性缺失或类型不匹配时不命中。
        miss = self.svc.evaluate("flag-a", {"age": "20"})
        self.assertEqual(miss["reason"], "disabled")

    def test_rollout_bucket_boundary(self):
        subject = "user-42"
        bucket = bucket_of("flag-a", "s1", subject)
        # percentage 恰好覆盖该 bucket：bucket < percentage * 100 成立。
        self.svc.publish("flag-a", 1, make_definition(
            rollout={"percentage": (bucket + 1) / 100, "salt": "s1", "serve": True}))
        result = self.svc.evaluate("flag-a", {"subject_id": subject})
        self.assertEqual(result["bucket"], bucket)
        self.assertEqual((result["enabled"], result["reason"]), (True, "rollout"))

        self.svc.publish("flag-a", 2, make_definition(
            rollout={"percentage": bucket / 100, "salt": "s1", "serve": True}))
        result = self.svc.evaluate("flag-a", {"subject_id": subject})
        self.assertEqual((result["enabled"], result["reason"]), (False, "default"))

    def test_rollout_percentage_zero_and_hundred(self):
        self.svc.publish("flag-zero", 1, make_definition(rollout={"percentage": 0, "salt": "s", "serve": True}))
        self.assertEqual(self.svc.evaluate("flag-zero", {"subject_id": "u"})["reason"], "default")
        self.svc.publish("flag-full", 1, make_definition(rollout={"percentage": 100, "salt": "s", "serve": True}))
        self.assertEqual(self.svc.evaluate("flag-full", {"subject_id": "u"})["reason"], "rollout")

    def test_missing_subject(self):
        self.svc.publish("flag-a", 1, make_definition())
        with self.assertRaises(MissingSubjectError):
            self.svc.evaluate("flag-a", {})
        with self.assertRaises(MissingSubjectError):
            self.svc.evaluate("flag-a", {"subject_id": ""})
        # 规则命中时不需要 subject_id。
        self.svc.publish("flag-b", 1, make_definition(
            rules=[{"attribute": "vip", "operator": "equals", "value": True, "serve": True}]))
        self.assertTrue(self.svc.evaluate("flag-b", {"vip": True})["enabled"])


class RollbackTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        # v1：全量关闭；v2：对 pro 用户开启。
        self.svc.publish("flag-a", 1, make_definition(enabled=False, default=False))
        self.svc.publish("flag-a", 2, make_definition(
            enabled=False,
            default=False,
            rules=[{"attribute": "plan", "operator": "equals", "value": "pro", "serve": True}],
        ))
        self.subjects = [
            {"subject_id": "u1", "plan": "pro"},
            {"subject_id": "u2", "plan": "free"},
            {"subject_id": "u3", "plan": "pro"},
        ]

    def test_rollback_conflict_keeps_current(self):
        with self.assertRaises(RollbackConflictError):
            self.svc.rollback("flag-a", 1, self.subjects, expected_impacted=[])
        # 当前版本未变。
        self.assertEqual(self.svc.evaluate("flag-a", self.subjects[0])["revision"], 2)

    def test_rollback_success_activates_target(self):
        impacted = self.svc.rollback("flag-a", 1, self.subjects, expected_impacted={"u1", "u3"})
        self.assertEqual(impacted, ["u1", "u3"])
        result = self.svc.evaluate("flag-a", self.subjects[0])
        self.assertEqual((result["revision"], result["reason"]), (1, "disabled"))

    def test_rollback_unknown_flag_or_revision(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.rollback("nope", 1, [], [])
        with self.assertRaises(RevisionNotFoundError):
            self.svc.rollback("flag-a", 99, [], [])


if __name__ == "__main__":
    unittest.main()
