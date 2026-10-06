"""回退最近确认阶段（rollback_stage）的单元测试。"""

import hashlib
import unittest

from flag_rollout import (
    FeatureFlagService,
    FlagNotFoundError,
    InvalidRolloutChangeError,
    MissingSubjectError,
    PrerequisiteCycleError,
    RevisionNotFoundError,
    RolloutPlanStateError,
    RolloutStageConflictError,
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


class RollbackStageTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        # v1：0% 放量；规则外主体全部走 default=False。
        self.svc.publish("flag-a", 1, make_definition())
        self.subjects = [{"subject_id": "u%d" % i} for i in range(40)]

    def impacted(self, old_pct, new_pct, flag_key="flag-a"):
        return {
            s["subject_id"] for s in self.subjects
            if old_pct * 100 <= bucket_of(flag_key, "s1", s["subject_id"])
            < new_pct * 100
        }

    def test_rollback_first_stage_restores_base_revision(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["flagKey"], "flag-a")
        self.assertEqual(result["rolledBackStage"],
                         {"name": "canary", "percentage": 50, "index": 0})
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["impacted"],
                         sorted(self.impacted(0, 50), key=str))
        self.assertEqual(result["cursor"], 0)
        self.assertFalse(result["completed"])
        self.assertEqual(result["remaining"], 2)
        # 当前 revision 回到基准；被回退阶段的历史 revision 仍可按 revision 求值。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        historical = self.svc.evaluate(
            "flag-a", {"subject_id": "u1"}, revision=2)
        self.assertEqual(historical["revision"], 2)
        # 后续阶段定义保留，计划可从首阶段重新推进。
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))

    def test_rollback_later_stage_restores_previous_result_revision(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "beta", "percentage": 90},
            {"name": "full", "percentage": 100},
        ])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 90))
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(50, 90))
        self.assertEqual(result["rolledBackStage"],
                         {"name": "beta", "percentage": 90, "index": 1})
        self.assertEqual(result["restoredRevision"], 2)
        self.assertEqual(result["cursor"], 1)
        self.assertFalse(result["completed"])
        self.assertEqual(result["remaining"], 2)
        # 当前 revision 是上一次推进的结果 revision=2（50% 版）。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)

    def test_rollback_final_stage_reopens_completed_plan(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))
        # 计划已完成，仍可回退末阶段；回退后 completed=False，末阶段待确认。
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(50, 100))
        self.assertEqual(result["rolledBackStage"],
                         {"name": "full", "percentage": 100, "index": 1})
        self.assertEqual(result["restoredRevision"], 2)
        self.assertEqual(result["cursor"], 1)
        self.assertFalse(result["completed"])
        self.assertEqual(result["remaining"], 1)

    def test_readvance_after_rollback_uses_next_unused_revision(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "beta", "percentage": 90},
            {"name": "full", "percentage": 100},
        ])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 90))
        self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(50, 90))
        # 重新推进被回退的 beta：不重用 revision=3，分配下一未用正整数 4。
        again = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 90))
        self.assertEqual(again["revision"], 4)
        self.assertEqual(again["stage"]["index"], 1)
        self.assertEqual(again["remaining"], 1)
        # 后续 full 阶段仍保留并可推进，继续分配 revision=5。
        last = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(90, 100))
        self.assertEqual(last["revision"], 5)
        self.assertTrue(last["completed"])
        # 被回退的旧 revision=3 仍可按 revision 求值。
        self.assertEqual(
            self.svc.evaluate(
                "flag-a", {"subject_id": "u1"}, revision=3)["revision"],
            3)

    def test_rollback_then_advance_first_stage_again(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        # 历史 revision=2 不重用，重新推进首阶段分配 revision=3。
        again = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(again["revision"], 3)
        self.assertEqual(again["stage"]["index"], 0)
        self.assertEqual(again["remaining"], 1)

    def test_duplicate_subject_ids_deduped_and_str_sorted(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        flipped = sorted(self.impacted(0, 50), key=str)
        subjects = ([{"subject_id": sid} for sid in flipped[:2]]
                    + [{"subject_id": flipped[0]}])
        result = self.svc.rollback_stage(
            "flag-a", subjects, set(flipped[:2]))
        self.assertEqual(result["impacted"], flipped[:2])
        self.assertEqual(result["cursor"], 0)

    def test_conflict_carries_sorted_impact_and_changes_nothing(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        with self.assertRaises(RolloutStageConflictError) as cm:
            self.svc.rollback_stage("flag-a", self.subjects, set())
        self.assertEqual(cm.exception.impacted,
                         sorted(self.impacted(0, 50), key=str))
        self.assertEqual(cm.exception.args[1],
                         sorted(self.impacted(0, 50), key=str))
        # 不改当前 revision 与计划：正确影响面可重试成功。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["rolledBackStage"]["name"], "a")

    def test_no_plan_cursor_zero_or_deviation_raises_state_error(self):
        # 无计划。
        with self.assertRaises(RolloutPlanStateError):
            self.svc.rollback_stage("flag-a", [], set())

        # 有计划但 cursor 为 0。
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        with self.assertRaises(RolloutPlanStateError):
            self.svc.rollback_stage("flag-a", self.subjects, set())

        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        # rollback 使当前 revision 偏离最近确认值。
        self.svc.rollback(
            "flag-a", 1, self.subjects, self.impacted(0, 50))
        with self.assertRaises(RolloutPlanStateError):
            self.svc.rollback_stage("flag-a", self.subjects, set())

        # publish 新版本同样构成偏离。
        self.svc.publish("flag-a", 5, make_definition())
        with self.assertRaises(RolloutPlanStateError):
            self.svc.rollback_stage("flag-a", self.subjects, set())

        # 状态错误不改版本或计划。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 5)

    def test_state_error_after_promote_deviation_changes_nothing(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        # promote_rollout 把当前 revision 推进到 3，偏离计划最近确认值 2。
        self.svc.promote_rollout(
            "flag-a", 100, self.subjects, self.impacted(50, 100))
        with self.assertRaises(RolloutPlanStateError):
            self.svc.rollback_stage("flag-a", self.subjects, set())
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 3)
        # rollback 消除偏离后，原计划的最近确认阶段仍可回退。
        self.svc.rollback(
            "flag-a", 2, self.subjects, self.impacted(50, 100))
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["cursor"], 0)

    def test_unknown_flag_raises_not_found(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.rollback_stage("nope", [], set())

    def test_invalid_subjects_or_expected(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.rollback_stage("flag-a", 42, set())
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.rollback_stage("flag-a", self.subjects, 42)
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.rollback_stage("flag-a", self.subjects, [["u1"]])
        # 上下文非 Mapping。
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.rollback_stage("flag-a", [42], set())
        # subject_id 不可哈希且必然翻转（取一个 50% 下命中的不可哈希 id）。
        unhashable = None
        for i in range(100):
            candidate = ["u%d" % i]
            if bucket_of("flag-a", "s1", candidate) < 5000:
                unhashable = candidate
                break
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.rollback_stage(
                "flag-a", [{"subject_id": unhashable}], set())
        # 参数错误后状态不变：当前仍是推进后的 revision=2，计划仍在。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["restoredRevision"], 1)

    def test_missing_subject_when_entering_rollout_branch(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        with self.assertRaises(MissingSubjectError):
            self.svc.rollback_stage("flag-a", [{}], set())
        with self.assertRaises(MissingSubjectError):
            self.svc.rollback_stage("flag-a", [{"subject_id": ""}], set())
        # 异常后不改版本与计划。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)

    def test_rollback_does_not_create_or_delete_revision(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        # 回退只恢复 revision，不新建也不删除：revision=2 仍可求值，
        # revision=3 不存在。
        self.assertEqual(
            self.svc.evaluate(
                "flag-a", {"subject_id": "u1"}, revision=2)["revision"],
            2)
        with self.assertRaises(RevisionNotFoundError):
            self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=3)

    def test_dependency_missing_revision_after_advance_raises_and_keeps_state(self):
        # 推进时 dep rev1 无其他依赖；推进后发布 dep rev2（当前版）固定引用
        # ghost 的缺失 revision，使回退求值期依赖解析抛 RevisionNotFoundError。
        self.svc.publish("ghost", 1, make_definition(
            rollout={"percentage": 0, "salt": "g", "serve": True}))
        self.svc.publish("dep", 1, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True}))
        self.svc.publish("f", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}]))
        self.svc.create_rollout_plan("f", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        subjects = [{"subject_id": "u%d" % i} for i in range(20)]
        canary = {
            s["subject_id"] for s in subjects
            if bucket_of("f", "s1", s["subject_id"]) < 5000
        }
        self.svc.advance_rollout_plan("f", subjects, canary)
        self.svc.publish("dep", 2, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True},
            prerequisites=[{"flagKey": "ghost", "revision": 9}]))
        with self.assertRaises(RevisionNotFoundError):
            self.svc.rollback_stage("f", subjects, canary)
        # 异常不改当前 revision 与计划（dep 当前版已坏，直接检查内部状态）。
        self.assertEqual(self.svc._flags["f"].current, 2)
        self.assertIn(2, self.svc._flags["f"].revisions)
        plan = self.svc._plans["f"]
        self.assertEqual(plan.cursor, 1)
        self.assertEqual(plan.confirmed_revision, 2)
        # 依赖修复（dep 当前版不再引用缺失版本）后相同输入可重试成功。
        self.svc.publish("dep", 3, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True}))
        result = self.svc.rollback_stage("f", subjects, canary)
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["cursor"], 0)

    def test_dependency_missing_subject_raises_and_keeps_state(self):
        # 主功能门控通过后依赖进入 100% 放量；回退传入缺 subject_id 的上下文，
        # 依赖侧抛 MissingSubjectError（主功能自身的放量分支尚未进入）。
        self.svc.publish("dep", 1, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True}))
        self.svc.publish("f", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}]))
        self.svc.create_rollout_plan("f", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        subjects = [{"subject_id": "u%d" % i} for i in range(20)]
        canary = {
            s["subject_id"] for s in subjects
            if bucket_of("f", "s1", s["subject_id"]) < 5000
        }
        self.svc.advance_rollout_plan("f", subjects, canary)
        with self.assertRaises(MissingSubjectError):
            self.svc.rollback_stage("f", [{}], set())
        with self.assertRaises(MissingSubjectError):
            self.svc.rollback_stage("f", [{"subject_id": ""}], set())
        plan = self.svc._plans["f"]
        self.assertEqual(plan.cursor, 1)
        self.assertEqual(plan.confirmed_revision, 2)
        # 补齐 subject_id 后相同阶段可正常回退。
        result = self.svc.rollback_stage("f", subjects, canary)
        self.assertEqual(result["restoredRevision"], 1)

    def test_dependency_cycle_raises_and_keeps_state(self):
        # 推进后把依赖当前版换成自环定义：回退求值依赖时即成环。
        self.svc.publish("dep", 1, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True}))
        self.svc.publish("f", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}]))
        self.svc.create_rollout_plan("f", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        subjects = [{"subject_id": "u%d" % i} for i in range(20)]
        canary = {
            s["subject_id"] for s in subjects
            if bucket_of("f", "s1", s["subject_id"]) < 5000
        }
        self.svc.advance_rollout_plan("f", subjects, canary)
        self.svc.publish("dep", 2, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True},
            prerequisites=[{"flagKey": "dep"}]))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.rollback_stage("f", subjects, set())
        plan = self.svc._plans["f"]
        self.assertEqual(plan.cursor, 1)
        self.assertEqual(plan.confirmed_revision, 2)
        # 环消除后可用相同输入重试成功。
        self.svc.publish("dep", 3, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True}))
        result = self.svc.rollback_stage("f", subjects, canary)
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["cursor"], 0)


if __name__ == "__main__":
    unittest.main()
