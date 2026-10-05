"""取消未完成放量计划（cancel_rollout_plan）的单元测试。"""

import hashlib
import unittest

from flag_rollout import (
    FeatureFlagService,
    FlagNotFoundError,
    InvalidRolloutChangeError,
    MissingSubjectError,
    RevisionNotFoundError,
    RolloutCancelConflictError,
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


class CancelRolloutPlanTest(unittest.TestCase):
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

    # ------------------------------------------------------------------
    # 成功路径
    # ------------------------------------------------------------------
    def test_cancel_before_any_advance_restores_base_with_none_stage(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        result = self.svc.cancel_rollout_plan("flag-a", self.subjects, set())
        self.assertEqual(result["flagKey"], "flag-a")
        self.assertEqual(result["restoredRevision"], 1)
        self.assertIsNone(result["cancelledStage"])
        self.assertEqual(result["impacted"], [])
        # 当前 revision 仍是基准 1。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        # 计划已删除：可立即在同一 flag_key 登记新计划。
        new_plan = self.svc.create_rollout_plan("flag-a", [
            {"name": "again", "percentage": 100}])
        self.assertEqual(new_plan["baseRevision"], 1)
        self.assertEqual(new_plan["stages"][0]["name"], "again")

    def test_cancel_after_advance_restores_base_and_reports_last_stage(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "beta", "percentage": 80},
            {"name": "full", "percentage": 100},
        ])
        first = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        second = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 80))
        self.assertEqual((first["revision"], second["revision"]), (2, 3))

        # 取消时影响面为当前 revision(3, 80%) 相对 base(1, 0%) 的全部 enabled 翻转。
        expected = self.impacted(0, 80)
        result = self.svc.cancel_rollout_plan("flag-a", self.subjects, expected)
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["cancelledStage"], "beta")
        self.assertEqual(result["impacted"], sorted(expected, key=str))

        # 当前 revision 回到基准；推进产生的历史 revision 仍可按 revision 求值。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        historical = self.svc.evaluate(
            "flag-a", {"subject_id": "u1"}, revision=3)
        self.assertEqual(historical["revision"], 3)
        # 取消不创建任何 revision：4 不存在。
        with self.assertRaises(RevisionNotFoundError):
            self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=4)
        # flag_key 立即可以新建计划。
        again = self.svc.create_rollout_plan("flag-a", [
            {"name": "x", "percentage": 100}])
        self.assertEqual(again["stages"][0]["name"], "x")

    def test_cancel_after_single_advance_stage_name_is_canary(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        result = self.svc.cancel_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["cancelledStage"], "canary")
        self.assertEqual(result["restoredRevision"], 1)

    def test_impact_deduped_and_str_sorted(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        candidates = ["u10", "u2", "u1", "u20"]
        flipped = {
            u for u in candidates if bucket_of("flag-a", "s1", u) < 5000
        }
        self.svc.advance_rollout_plan(
            "flag-a", [{"subject_id": u} for u in candidates], flipped)
        result = self.svc.cancel_rollout_plan(
            "flag-a",
            [{"subject_id": u} for u in candidates] * 2,
            flipped)
        self.assertEqual(result["impacted"], sorted(flipped, key=str))

    def test_subjects_accepts_any_iterable(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        expected = {"u1"} if bucket_of("flag-a", "s1", "u1") < 5000 else set()
        self.svc.advance_rollout_plan(
            "flag-a", ({"subject_id": "u1"} for _ in range(1)), expected)
        result = self.svc.cancel_rollout_plan(
            "flag-a",
            ({"subject_id": "u1"} for _ in range(1)),
            (x for x in expected))
        self.assertEqual(result["impacted"], sorted(expected, key=str))

    def test_impact_uses_rule_enabled_rollout_order(self):
        # base 与推进版本仅 rollout.percentage 不同；规则命中的主体两边都取
        # rule.serve，不计入影响。
        self.svc.publish("flag-r", 1, make_definition(
            rules=[{"attribute": "vip", "operator": "equals",
                    "value": True, "serve": True}]))
        self.svc.create_rollout_plan("flag-r", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        flipped = {"u2"} if bucket_of("flag-r", "s1", "u2") < 5000 else set()
        self.svc.advance_rollout_plan(
            "flag-r",
            [{"subject_id": "u1", "vip": True}, {"subject_id": "u2"}],
            flipped)
        result = self.svc.cancel_rollout_plan(
            "flag-r",
            [{"subject_id": "u1", "vip": True}, {"subject_id": "u2"}],
            flipped)
        self.assertEqual(result["impacted"], sorted(flipped, key=str))

    # ------------------------------------------------------------------
    # 状态错误
    # ------------------------------------------------------------------
    def test_no_plan_raises_state_error(self):
        with self.assertRaises(RolloutPlanStateError):
            self.svc.cancel_rollout_plan("flag-a", [], set())
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)

    def test_completed_plan_raises_state_error(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", [{"subject_id": "u1"}], {"u1"})
        with self.assertRaises(RolloutPlanStateError):
            self.svc.cancel_rollout_plan("flag-a", [{"subject_id": "u1"}], set())
        # 完成的计划不被删除，当前版本仍是推进后的 revision=2。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)

    def test_revision_deviation_raises_state_error_and_changes_nothing(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        # rollback 使当前 revision 偏离最近确认值。
        self.svc.rollback(
            "flag-a", 1, self.subjects, self.impacted(0, 50))
        with self.assertRaises(RolloutPlanStateError):
            self.svc.cancel_rollout_plan("flag-a", self.subjects, set())

        # publish 新版本同样构成偏离。
        self.svc.publish("flag-a", 5, make_definition())
        with self.assertRaises(RolloutPlanStateError):
            self.svc.cancel_rollout_plan("flag-a", self.subjects, set())

        # promote_rollout 偏离最近确认值。
        svc2 = FeatureFlagService()
        svc2.publish("f", 1, make_definition())
        svc2.create_rollout_plan("f", [{"name": "a", "percentage": 50}])
        svc2.promote_rollout(
            "f", 100, [{"subject_id": "u1"}], {"u1"})
        with self.assertRaises(RolloutPlanStateError):
            svc2.cancel_rollout_plan("f", [{"subject_id": "u1"}], set())
        self.assertEqual(svc2.evaluate("f", {"subject_id": "u1"})["revision"], 2)

    def test_unknown_flag_raises_not_found(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.cancel_rollout_plan("nope", [], set())

    # ------------------------------------------------------------------
    # 输入错误
    # ------------------------------------------------------------------
    def test_invalid_subjects_or_expected(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan("flag-a", 42, set())
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan(
                "flag-a", self.subjects, 42)
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan(
                "flag-a", self.subjects, [["u1"]])
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan("flag-a", [42], set())
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan("flag-a", [None], set())
        # subject_id 不可哈希且在当前 50% 相对基准 0% 下必然翻转：
        # 确定性挑一个落在放量桶内的列表型 subject_id。
        unhashable = next(
            ["u%d" % i] for i in range(1000)
            if bucket_of("flag-a", "s1", ["u%d" % i]) < 5000
        )
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan(
                "flag-a", [{"subject_id": unhashable}], set())
        # 参数错误后状态不变：当前仍是 revision=2，计划仍可取消。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        result = self.svc.cancel_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["restoredRevision"], 1)

    def test_missing_subject_when_entering_rollout_branch(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 100}])
        # 未推进时当前 revision 即基准，放量分支同样要求非空 subject_id。
        with self.assertRaises(MissingSubjectError):
            self.svc.cancel_rollout_plan("flag-a", [{}], set())
        with self.assertRaises(MissingSubjectError):
            self.svc.cancel_rollout_plan("flag-a", [{"subject_id": ""}], set())

        # 推进后取消，当前 revision(50%) 下缺 subject_id 同样抛 MissingSubjectError。
        svc2 = FeatureFlagService()
        svc2.publish("f", 1, make_definition())
        svc2.create_rollout_plan("f", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        first_expected = {"u1"} if bucket_of("f", "s1", "u1") < 5000 else set()
        svc2.advance_rollout_plan("f", [{"subject_id": "u1"}], first_expected)
        with self.assertRaises(MissingSubjectError):
            svc2.cancel_rollout_plan("f", [{}], set())
        # 异常后当前版本、cursor、confirmed_revision、计划均不变：
        # 当前仍是 revision=2，cursor 仍为 1，可继续推进第二阶段。
        self.assertEqual(
            svc2.evaluate("f", {"subject_id": "u1"})["revision"], 2)
        svc2.advance_rollout_plan(
            "f", [{"subject_id": "u1"}], {"u1"} - first_expected)
        self.assertEqual(
            svc2.evaluate("f", {"subject_id": "u1"})["revision"], 3)

    # ------------------------------------------------------------------
    # 影响面冲突
    # ------------------------------------------------------------------
    def test_conflict_carries_sorted_impact_and_changes_nothing(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        with self.assertRaises(RolloutCancelConflictError) as cm:
            self.svc.cancel_rollout_plan("flag-a", self.subjects, set())
        self.assertEqual(
            cm.exception.args[1], sorted(self.impacted(0, 50), key=str))

        # 冲突不改当前 revision、cursor、confirmed_revision 与计划：
        # 当前仍是 revision=2，可用正确影响面重试取消成功。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        result = self.svc.cancel_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["cancelledStage"], "a")

    def test_conflict_keeps_cursor_and_confirmed_revision(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        with self.assertRaises(RolloutCancelConflictError):
            self.svc.cancel_rollout_plan("flag-a", self.subjects, set())
        # cursor 仍为 1：冲突后可正常推进第二阶段。
        second = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))
        self.assertEqual(second["revision"], 3)
        self.assertEqual(second["stage"]["name"], "b")
        self.assertTrue(second["completed"])

    def test_cancelled_flag_key_released_for_new_plan(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 100}])
        # 未推进直接取消。
        self.svc.cancel_rollout_plan("flag-a", [], set())
        # 不抛 RolloutPlanConflictError 即说明 flag_key 已释放。
        plan = self.svc.create_rollout_plan("flag-a", [
            {"name": "b", "percentage": 100}])
        self.assertEqual(plan["stages"][0]["name"], "b")


if __name__ == "__main__":
    unittest.main()
