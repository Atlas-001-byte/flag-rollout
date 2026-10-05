"""多阶段放量计划（create_rollout_plan / advance_rollout_plan）的单元测试。"""

import hashlib
import unittest

from flag_rollout import (
    FeatureFlagService,
    FlagNotFoundError,
    InvalidRolloutChangeError,
    InvalidRolloutPlanError,
    MissingSubjectError,
    RolloutConflictError,
    RolloutPlanConflictError,
    RolloutPlanStateError,
)


def bucket_of(flag_key, salt, subject_id):
    digest = hashlib.sha256(("%s:%s:%s" % (flag_key, salt, subject_id)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 10000


def make_definition(**overrides):
    definition = {
        "enabled": True,
        "default": False,
        "rules": [],
        "rollout": {"percentage": 10, "salt": "s1", "serve": True},
    }
    definition.update(overrides)
    return definition


def impacted_between(flag_key, salt, subject_ids, low, high):
    """bucket 落在 [low*100, high*100) 的主体在 low->high 晋升中 enabled 翻转。"""
    return {
        sid for sid in subject_ids
        if low * 100 <= bucket_of(flag_key, salt, sid) < high * 100
    }


class CreateRolloutPlanTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        self.svc.publish("flag-a", 1, make_definition())

    def test_create_returns_plan_snapshot(self):
        plan = self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 25},
            {"name": "beta", "percentage": 50.5},
            {"name": "ga", "percentage": 100},
        ])
        self.assertEqual(plan["flagKey"], "flag-a")
        self.assertEqual(plan["baseRevision"], 1)
        self.assertEqual(plan["basePercentage"], 10)
        self.assertEqual(plan["stages"], [
            {"name": "canary", "percentage": 25, "index": 0},
            {"name": "beta", "percentage": 50.5, "index": 1},
            {"name": "ga", "percentage": 100, "index": 2},
        ])

    def test_create_does_not_change_revision_or_prebuild_versions(self):
        self.svc.create_rollout_plan("flag-a", [{"name": "ga", "percentage": 100}])
        # 当前版本不变，evaluate 仍按 10% 求值。
        result = self.svc.evaluate("flag-a", {"subject_id": "u1"})
        self.assertEqual(result["revision"], 1)
        # 未预建版本：promote 仍从 max+1=2 开始。
        promoted = self.svc.promote_rollout("flag-a", 20, [], set())
        self.assertEqual(promoted["revision"], 2)

    def test_unknown_flag(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.create_rollout_plan("nope", [{"name": "a", "percentage": 50}])

    def test_invalid_stages(self):
        bad_stages = [
            None,  # 不可迭代
            42,
            [],  # 空
            [50],  # 非 Mapping
            [{"percentage": 50}],  # 缺 name
            [{"name": "", "percentage": 50}],  # name 空
            [{"name": 1, "percentage": 50}],  # name 非字符串
            [{"name": "a"}],  # 缺 percentage
            [{"name": "a", "percentage": True}],  # 布尔
            [{"name": "a", "percentage": "50"}],  # 非数值
            [{"name": "a", "percentage": float("nan")}],  # 非有限
            [{"name": "a", "percentage": float("inf")}],
            [{"name": "a", "percentage": 101}],  # 超 100
            [{"name": "a", "percentage": 10}],  # 不高于当前 10
            [{"name": "a", "percentage": 5}],  # 低于当前
            [{"name": "a", "percentage": 50}, {"name": "a", "percentage": 60}],  # 重名
            [{"name": "a", "percentage": 50}, {"name": "b", "percentage": 50}],  # 非严格递增
            [{"name": "a", "percentage": 60}, {"name": "b", "percentage": 50}],  # 递减
        ]
        for i, bad in enumerate(bad_stages):
            with self.assertRaises(InvalidRolloutPlanError, msg="case %d: %r" % (i, bad)):
                self.svc.create_rollout_plan("flag-a", bad)

    def test_invalid_create_does_not_register_plan(self):
        with self.assertRaises(InvalidRolloutPlanError):
            self.svc.create_rollout_plan("flag-a", [])
        # 失败的创建不占用计划位：随后可正常创建。
        plan = self.svc.create_rollout_plan("flag-a", [{"name": "ga", "percentage": 100}])
        self.assertEqual(plan["flagKey"], "flag-a")

    def test_conflict_while_plan_unfinished(self):
        self.svc.create_rollout_plan("flag-a", [{"name": "ga", "percentage": 100}])
        with self.assertRaises(RolloutPlanConflictError):
            self.svc.create_rollout_plan("flag-a", [{"name": "x", "percentage": 50}])

    def test_recreate_allowed_after_completion(self):
        self.svc.create_rollout_plan("flag-a", [{"name": "half", "percentage": 50}])
        self.svc.advance_rollout_plan("flag-a", [], set())
        plan = self.svc.create_rollout_plan("flag-a", [{"name": "ga", "percentage": 100}])
        self.assertEqual(plan["baseRevision"], 2)
        self.assertEqual(plan["basePercentage"], 50)


class AdvanceRolloutPlanTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        self.svc.publish("flag-a", 1, make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True}))
        self.subject_ids = ["u1", "u2", "u3", "u4", "u5"]
        self.subjects = [{"subject_id": sid} for sid in self.subject_ids]

    def _create_plan(self, stages=(("canary", 50), ("ga", 100))):
        return self.svc.create_rollout_plan(
            "flag-a", [{"name": n, "percentage": p} for n, p in stages])

    def test_advance_through_all_stages(self):
        self._create_plan()
        expected_1 = impacted_between("flag-a", "s1", self.subject_ids, 0, 50)
        result = self.svc.advance_rollout_plan("flag-a", self.subjects, expected_1)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["stage"], {"name": "canary", "percentage": 50, "index": 0})
        self.assertEqual(result["impacted"], sorted(expected_1, key=str))
        self.assertFalse(result["completed"])
        self.assertEqual(result["remaining"], 1)
        # 新版本已激活，旧版本求值不变。
        self.assertEqual(self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        old = self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=1)
        self.assertEqual((old["reason"], old["enabled"]), ("default", False))

        expected_2 = impacted_between("flag-a", "s1", self.subject_ids, 50, 100)
        result = self.svc.advance_rollout_plan("flag-a", self.subjects, expected_2)
        self.assertEqual(result["revision"], 3)
        self.assertEqual(result["stage"], {"name": "ga", "percentage": 100, "index": 1})
        self.assertEqual(result["impacted"], sorted(expected_2, key=str))
        self.assertTrue(result["completed"])
        self.assertEqual(result["remaining"], 0)

    def test_advance_new_revision_is_max_plus_one(self):
        # 非连续 revision：已有 1 与 3，当前为 3。
        self.svc.publish("flag-a", 3, make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True}))
        self._create_plan()
        expected = impacted_between("flag-a", "s1", self.subject_ids, 0, 50)
        result = self.svc.advance_rollout_plan("flag-a", self.subjects, expected)
        self.assertEqual(result["revision"], 4)

    def test_advance_only_copies_percentage(self):
        self.svc.publish("flag-b", 1, make_definition(
            rules=[{"attribute": "vip", "operator": "equals", "value": True, "serve": True}],
            rollout={"percentage": 0, "salt": "s1", "serve": True}))
        self.svc.create_rollout_plan("flag-b", [{"name": "ga", "percentage": 100}])
        subjects = [{"subject_id": "u1", "vip": True}, {"subject_id": "u2"}]
        # 规则命中者 enabled 不变，仅 u2 翻转；其余字段（rules/salt/serve）保持。
        result = self.svc.advance_rollout_plan("flag-b", subjects, {"u2"})
        self.assertEqual(result["impacted"], ["u2"])
        evaluated = self.svc.evaluate("flag-b", {"subject_id": "u1", "vip": True})
        self.assertEqual((evaluated["reason"], evaluated["enabled"]), ("rule", True))

    def test_no_plan_raises_state_error(self):
        with self.assertRaises(RolloutPlanStateError):
            self.svc.advance_rollout_plan("flag-a", self.subjects, set())

    def test_completed_plan_raises_state_error(self):
        self._create_plan((("ga", 100),))
        expected = impacted_between("flag-a", "s1", self.subject_ids, 0, 100)
        self.svc.advance_rollout_plan("flag-a", self.subjects, expected)
        with self.assertRaises(RolloutPlanStateError):
            self.svc.advance_rollout_plan("flag-a", self.subjects, set())

    def test_revision_drift_raises_state_error(self):
        self._create_plan()
        # 计划外操作使当前 revision 偏离最近确认值。
        self.svc.promote_rollout("flag-a", 30, self.subjects,
                                 impacted_between("flag-a", "s1", self.subject_ids, 0, 30))
        with self.assertRaises(RolloutPlanStateError):
            self.svc.advance_rollout_plan("flag-a", self.subjects, set())
        # 计划未被推进：偏离错误不消耗阶段，也不改版本。
        self.assertEqual(self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)

    def test_revision_drift_after_partial_advance(self):
        self._create_plan()
        expected_1 = impacted_between("flag-a", "s1", self.subject_ids, 0, 50)
        self.svc.advance_rollout_plan("flag-a", self.subjects, expected_1)
        # 推进一阶段后 rollback 偏离确认值。
        self.svc.rollback("flag-a", 1, self.subjects, expected_1)
        with self.assertRaises(RolloutPlanStateError):
            self.svc.advance_rollout_plan("flag-a", self.subjects, set())

    def test_impact_mismatch_conflict(self):
        self._create_plan()
        with self.assertRaises(RolloutConflictError) as cm:
            self.svc.advance_rollout_plan("flag-a", self.subjects, {"u1"})
        expected = impacted_between("flag-a", "s1", self.subject_ids, 0, 50)
        # 异常参数携带排序后的实际影响列表。
        self.assertEqual(cm.exception.args[1], sorted(expected, key=str))
        # 不建版本、不推进：当前版本不变，重试仍落在同一阶段同一 revision。
        self.assertEqual(self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        result = self.svc.advance_rollout_plan("flag-a", self.subjects, expected)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["stage"]["name"], "canary")

    def test_unknown_flag(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.advance_rollout_plan("nope", [], set())

    def test_non_iterable_subjects_or_expected(self):
        self._create_plan()
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.advance_rollout_plan("flag-a", 42, set())
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.advance_rollout_plan("flag-a", self.subjects, 42)

    def test_unhashable_members(self):
        self._create_plan()
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.advance_rollout_plan("flag-a", self.subjects, [["u1"]])

    def test_unhashable_subject_id(self):
        # subject_id 不可哈希且 enabled 翻转（单阶段直接到 100%）。
        self.svc.publish("flag-b", 1, make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True}))
        self.svc.create_rollout_plan("flag-b", [{"name": "ga", "percentage": 100}])
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.advance_rollout_plan("flag-b", [{"subject_id": ["u1"]}], set())
        # 校验失败不改版本或计划。
        self.assertEqual(self.svc.evaluate("flag-b", {"subject_id": "u1"})["revision"], 1)

    def test_missing_subject(self):
        self._create_plan()
        with self.assertRaises(MissingSubjectError):
            self.svc.advance_rollout_plan("flag-a", [{}], set())
        with self.assertRaises(MissingSubjectError):
            self.svc.advance_rollout_plan("flag-a", [{"subject_id": ""}], set())
        self.assertEqual(self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)

    def test_duplicate_subject_ids_merged(self):
        self._create_plan((("ga", 100),))
        subjects = [{"subject_id": "u1"}, {"subject_id": "u1"}, {"subject_id": "u2"}]
        result = self.svc.advance_rollout_plan("flag-a", subjects, {"u1", "u2"})
        self.assertEqual(result["impacted"], ["u1", "u2"])

    def test_existing_entrypoints_unchanged_without_plan(self):
        # 无计划时 promote/rollback 行为与基线一致。
        result = self.svc.promote_rollout("flag-a", 100, self.subjects, set(self.subject_ids))
        self.assertEqual(result, {"revision": 2, "impacted": sorted(self.subject_ids)})
        impacted = self.svc.rollback("flag-a", 1, self.subjects, set(self.subject_ids))
        self.assertEqual(impacted, sorted(self.subject_ids))


if __name__ == "__main__":
    unittest.main()
