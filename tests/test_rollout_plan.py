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
    digest = hashlib.sha256(
        ("%s:%s:%s" % (flag_key, salt, subject_id)).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10000


def make_definition(**overrides):
    definition = {
        "enabled": True,
        "default": False,
        "rules": [],
        "rollout": {"percentage": 0, "salt": "s1", "serve": True},
    }
    definition.update(overrides)
    return definition


class CreateRolloutPlanTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        self.svc.publish("flag-a", 1, make_definition())
        self.svc.publish("flag-b", 2, make_definition(
            rollout={"percentage": 25, "salt": "s1", "serve": True}))

    def test_returns_base_and_indexed_stages_in_order(self):
        result = self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 10},
            {"name": "beta", "percentage": 50.5},
            {"name": "full", "percentage": 100},
        ])
        self.assertEqual(result["flagKey"], "flag-a")
        self.assertEqual(result["baseRevision"], 1)
        self.assertEqual(result["basePercentage"], 0)
        self.assertEqual(result["stages"], [
            {"name": "canary", "percentage": 10, "index": 0},
            {"name": "beta", "percentage": 50.5, "index": 1},
            {"name": "full", "percentage": 100, "index": 2},
        ])

    def test_base_uses_current_revision_percentage(self):
        result = self.svc.create_rollout_plan("flag-b", [
            {"name": "a", "percentage": 50}])
        self.assertEqual(result["baseRevision"], 2)
        self.assertEqual(result["basePercentage"], 25)

    def test_plan_does_not_change_revision_or_prebuild_versions(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 10}, {"name": "b", "percentage": 100}])
        # 当前 revision 仍是 1，且没有预建 revision=2。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        with self.assertRaises(Exception):
            self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=2)

    def test_stages_accept_any_iterable(self):
        result = self.svc.create_rollout_plan(
            "flag-a", ({"name": "a", "percentage": 10} for _ in range(1)))
        self.assertEqual(result["stages"],
                         [{"name": "a", "percentage": 10, "index": 0}])

    def test_unknown_flag_raises_not_found(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.create_rollout_plan("nope", [{"name": "a", "percentage": 10}])

    def test_invalid_stages(self):
        bad_stages = [
            [],
            None,
            42,
            [None],
            ["x"],
            [{}],
            [{"percentage": 10}],
            [{"name": "", "percentage": 10}],
            [{"name": 7, "percentage": 10}],
            [{"name": "a", "percentage": 10}, {"name": "a", "percentage": 20}],
            [{"name": "a"}],  # 缺 percentage
            [{"name": "a", "percentage": True}],
            [{"name": "a", "percentage": False}],
            [{"name": "a", "percentage": "10"}],
            [{"name": "a", "percentage": None}],
            [{"name": "a", "percentage": float("nan")}],
            [{"name": "a", "percentage": float("inf")}],
            [{"name": "a", "percentage": -float("inf")}],
            [{"name": "a", "percentage": 101}],
            [{"name": "a", "percentage": -1}],
            [{"name": "a", "percentage": 0}],          # 未严格高于当前 0
            [{"name": "a", "percentage": 25}],         # 未严格高于 flag-b 当前 25
            [{"name": "a", "percentage": 50},
             {"name": "b", "percentage": 50}],         # 未严格递增
            [{"name": "a", "percentage": 50},
             {"name": "b", "percentage": 49}],
        ]
        for i, bad in enumerate(bad_stages):
            flag = "flag-b" if (isinstance(bad, list) and bad
                                and isinstance(bad[0], dict)
                                and bad[0].get("percentage") == 25) else "flag-a"
            with self.assertRaises(InvalidRolloutPlanError, msg="case %d %r" % (i, bad)):
                self.svc.create_rollout_plan(flag, bad)

    def test_invalid_plan_does_not_register(self):
        with self.assertRaises(InvalidRolloutPlanError):
            self.svc.create_rollout_plan("flag-a", [])
        # 非法登记之后，合法登记不应当被冲突挡下。
        result = self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 10}])
        self.assertEqual(result["stages"][0]["index"], 0)

    def test_strictly_above_current_float_boundary_accepted(self):
        result = self.svc.create_rollout_plan("flag-b", [
            {"name": "a", "percentage": 25.0001}])
        self.assertEqual(result["stages"][0]["percentage"], 25.0001)

    def test_unfinished_plan_conflicts(self):
        self.svc.create_rollout_plan("flag-a", [{"name": "a", "percentage": 10}])
        with self.assertRaises(RolloutPlanConflictError):
            self.svc.create_rollout_plan("flag-a", [{"name": "b", "percentage": 20}])
        # 冲突不覆盖原计划：推进仍按原阶段 percentage=10 执行。
        subjects = [{"subject_id": "u1"}]
        expected = {"u1"} if bucket_of("flag-a", "s1", "u1") < 1000 else set()
        result = self.svc.advance_rollout_plan("flag-a", subjects, expected)
        self.assertEqual(result["stage"]["percentage"], 10)

    def test_completed_plan_allows_new_registration(self):
        subjects = [{"subject_id": "u1"}, {"subject_id": "u2"}]
        self.svc.create_rollout_plan("flag-a", [{"name": "a", "percentage": 100}])
        expected = {"u1", "u2"}
        result = self.svc.advance_rollout_plan("flag-a", subjects, expected)
        self.assertTrue(result["completed"])
        # 当前已 100%，无法直接再登记；回滚到基准后登记新计划应被允许。
        self.svc.rollback("flag-a", 1, subjects, expected)
        new_plan = self.svc.create_rollout_plan("flag-a", [
            {"name": "again", "percentage": 100}])
        self.assertEqual(new_plan["baseRevision"], 1)
        self.assertEqual(new_plan["stages"][0]["name"], "again")


class AdvanceRolloutPlanTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        # v1：0% 放量；规则外主体全部走 default=False。
        self.svc.publish("flag-a", 1, make_definition())
        self.subjects = [{"subject_id": "u%d" % i} for i in range(40)]

    def impacted(self, old_pct, new_pct):
        return {
            s["subject_id"] for s in self.subjects
            if old_pct * 100 <= bucket_of("flag-a", "s1", s["subject_id"]) < new_pct * 100
        }

    def test_advance_creates_revision_and_reports_stage_progress(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        first = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(first["revision"], 2)
        self.assertEqual(first["stage"],
                         {"name": "canary", "percentage": 50, "index": 0})
        self.assertFalse(first["completed"])
        self.assertEqual(first["remaining"], 1)
        self.assertEqual(first["impacted"], sorted(self.impacted(0, 50), key=str))

        second = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))
        self.assertEqual(second["revision"], 3)
        self.assertEqual(second["stage"],
                         {"name": "full", "percentage": 100, "index": 1})
        self.assertTrue(second["completed"])
        self.assertEqual(second["remaining"], 0)
        # 影响是相对当前阶段的增量，且按字符串排序。
        self.assertEqual(second["impacted"], sorted(self.impacted(50, 100), key=str))

    def test_candidate_only_changes_rollout_percentage(self):
        self.svc.publish("flag-r", 1, make_definition(
            rules=[{"attribute": "vip", "operator": "equals",
                    "value": True, "serve": True}]))
        self.svc.create_rollout_plan("flag-r", [{"name": "a", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-r", [{"subject_id": "u1", "vip": True}, {"subject_id": "u2"}],
            {"u2"})
        result = self.svc.evaluate("flag-r", {"subject_id": "u2"})
        self.assertEqual((result["revision"], result["reason"]), (2, "rollout"))
        vip = self.svc.evaluate("flag-r", {"subject_id": "u1", "vip": True})
        self.assertEqual(vip["reason"], "rule")
        # salt 与规则等其余字段保持不变。
        self.assertEqual(
            self.svc.evaluate("flag-r", {"subject_id": "u2"})["bucket"],
            bucket_of("flag-r", "s1", "u2"))

    def test_next_revision_is_max_plus_one_with_gaps(self):
        self.svc.publish("flag-a", 3, make_definition())
        self.svc.create_rollout_plan("flag-a", [{"name": "a", "percentage": 100}])
        result = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, {s["subject_id"] for s in self.subjects})
        self.assertEqual(result["revision"], 4)

    def test_duplicate_subject_ids_deduped_and_str_sorted(self):
        self.svc.create_rollout_plan("flag-a", [{"name": "a", "percentage": 100}])
        subjects = [{"subject_id": "u10"}, {"subject_id": "u2"},
                    {"subject_id": "u2"}, {"subject_id": "u1"}]
        result = self.svc.advance_rollout_plan(
            "flag-a", subjects, {"u10", "u2", "u1"})
        self.assertEqual(result["impacted"], ["u1", "u10", "u2"])

    def test_conflict_carries_sorted_impact_and_changes_nothing(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        with self.assertRaises(RolloutConflictError) as cm:
            self.svc.advance_rollout_plan("flag-a", self.subjects, set())
        self.assertEqual(cm.exception.args[1], sorted(self.impacted(0, 50), key=str))
        # 不建版本、不推进：当前仍是 revision=1，且正确影响面可重试成功。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        result = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["stage"]["index"], 0)

    def test_no_plan_completed_or_deviation_raises_state_error(self):
        # 无计划。
        with self.assertRaises(RolloutPlanStateError):
            self.svc.advance_rollout_plan("flag-a", [], set())

        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        # rollback 使当前 revision 偏离最近确认值。
        self.svc.rollback(
            "flag-a", 1, self.subjects, self.impacted(0, 50))
        with self.assertRaises(RolloutPlanStateError):
            self.svc.advance_rollout_plan("flag-a", self.subjects, set())

        # publish 新版本同样构成偏离。
        self.svc.publish("flag-a", 5, make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True}))
        with self.assertRaises(RolloutPlanStateError):
            self.svc.advance_rollout_plan("flag-a", self.subjects, set())

        # 计划完成后再推进。
        self.svc2 = FeatureFlagService()
        self.svc2.publish("f", 1, make_definition())
        self.svc2.create_rollout_plan("f", [{"name": "a", "percentage": 100}])
        self.svc2.advance_rollout_plan(
            "f", [{"subject_id": "u1"}], {"u1"})
        with self.assertRaises(RolloutPlanStateError):
            self.svc2.advance_rollout_plan("f", [{"subject_id": "u1"}], set())

    def test_state_error_changes_nothing(self):
        self.svc.create_rollout_plan("flag-a", [{"name": "a", "percentage": 50}])
        # promote_rollout 把当前 revision 推进到 2，偏离计划最近确认值 1。
        self.svc.promote_rollout(
            "flag-a", 100, self.subjects,
            {s["subject_id"] for s in self.subjects})
        with self.assertRaises(RolloutPlanStateError):
            self.svc.advance_rollout_plan("flag-a", self.subjects, set())
        # 状态错误不建版本、不推进：当前仍是 revision=2。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)

    def test_unknown_flag_raises_not_found(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.advance_rollout_plan("nope", [], set())

    def test_invalid_subjects_or_expected(self):
        self.svc.create_rollout_plan("flag-a", [{"name": "a", "percentage": 100}])
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.advance_rollout_plan("flag-a", 42, set())
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.advance_rollout_plan("flag-a", self.subjects, 42)
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.advance_rollout_plan(
                "flag-a", self.subjects, [["u1"]])
        # subject_id 不可哈希且在 100% 下必然翻转。
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.advance_rollout_plan(
                "flag-a", [{"subject_id": ["u1"]}], set())
        # 参数错误后状态不变。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)

    def test_missing_subject_when_entering_rollout_branch(self):
        self.svc.create_rollout_plan("flag-a", [{"name": "a", "percentage": 100}])
        with self.assertRaises(MissingSubjectError):
            self.svc.advance_rollout_plan("flag-a", [{}], set())
        with self.assertRaises(MissingSubjectError):
            self.svc.advance_rollout_plan("flag-a", [{"subject_id": ""}], set())
        # 异常后不建版本、不推进。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        self.svc.advance_rollout_plan(
            "flag-a", [{"subject_id": "u1"}], {"u1"})

    def test_existing_entrypoints_unchanged_without_plan(self):
        # 没有计划时 promote_rollout / rollback 行为与基线一致。
        result = self.svc.promote_rollout(
            "flag-a", 100, self.subjects,
            {s["subject_id"] for s in self.subjects})
        self.assertEqual(result["revision"], 2)
        impacted = self.svc.rollback(
            "flag-a", 1, self.subjects,
            {s["subject_id"] for s in self.subjects})
        self.assertEqual(len(impacted), len(self.subjects))
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)


if __name__ == "__main__":
    unittest.main()
