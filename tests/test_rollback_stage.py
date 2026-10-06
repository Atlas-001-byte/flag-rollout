"""回退放量阶段（rollback_stage）的单元测试。"""

import hashlib
import unittest

from flag_rollout import (
    FeatureFlagService,
    FlagNotFoundError,
    InvalidRolloutChangeError,
    MissingSubjectError,
    PrerequisiteCycleError,
    RevisionNotFoundError,
    RolloutStageConflictError,
    RolloutStageStateError,
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
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])

    def impacted(self, old_pct, new_pct):
        return {
            s["subject_id"] for s in self.subjects
            if old_pct * 100 <= bucket_of("flag-a", "s1", s["subject_id"]) < new_pct * 100
        }

    def advance_all(self):
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))

    def test_rollback_first_stage_restores_base_revision(self):
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        expected = self.impacted(0, 50)
        result = self.svc.rollback_stage("flag-a", self.subjects, expected)
        self.assertEqual(result["flagKey"], "flag-a")
        self.assertEqual(result["rolledBackStage"],
                         {"name": "canary", "percentage": 50, "index": 0})
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["impacted"], sorted(expected, key=str))
        self.assertEqual(result["cursor"], 0)
        self.assertFalse(result["completed"])
        self.assertEqual(result["remaining"], 2)
        # 当前 revision 回到基准；被回退阶段的 revision 仍保留可按 revision 求值。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        historical = self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=2)
        self.assertEqual(historical["revision"], 2)

    def test_rollback_later_stage_restores_previous_stage_result(self):
        self.advance_all()
        expected = self.impacted(50, 100)
        result = self.svc.rollback_stage("flag-a", self.subjects, expected)
        # 目标为上次推进的结果（revision=2），而非基准 revision=1。
        self.assertEqual(result["restoredRevision"], 2)
        self.assertEqual(result["rolledBackStage"],
                         {"name": "full", "percentage": 100, "index": 1})
        self.assertEqual(result["cursor"], 1)
        self.assertFalse(result["completed"])
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        # 可继续向前回退一个阶段，恢复到基准。
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["rolledBackStage"]["name"], "canary")
        self.assertEqual(result["cursor"], 0)
        self.assertEqual(result["remaining"], 2)

    def test_rollback_last_stage_reopens_completed_plan(self):
        self.advance_all()
        # 计划完成后不能推进，但可以回退末阶段。
        with self.assertRaises(Exception):
            self.svc.advance_rollout_plan("flag-a", self.subjects, set())
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(50, 100))
        self.assertFalse(result["completed"])
        self.assertEqual(result["remaining"], 1)
        # 再次推进被回退的末阶段，计划重新完成。
        again = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))
        self.assertTrue(again["completed"])
        self.assertEqual(again["remaining"], 0)
        self.assertEqual(again["stage"]["name"], "full")

    def test_readvance_allocates_next_unused_positive_revision(self):
        # 回退首阶段后再推进：revision=2 已存在，必须分配下一未用正整数 3。
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        again = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(again["revision"], 3)
        self.assertEqual(again["stage"]["index"], 0)
        self.assertEqual(again["remaining"], 1)
        # 回退末阶段后重推：先推进末阶段（rev4），回退后再推分配 rev5。
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))
        self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(50, 100))
        re_last = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))
        self.assertEqual(re_last["revision"], 5)

    def test_target_revision_tracked_by_plan_despite_revision_gaps(self):
        # 首阶段推进后外部发布 revision=9 造成偏离，再用 rollback 消除偏离；
        # 第二阶段推进会分配 revision=10，回退第二阶段时目标必须是计划记录的
        # 上一阶段结果 revision=2，而不是 max 系列推算的其他版本。
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.publish("flag-a", 9, make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True}))
        with self.assertRaises(RolloutStageStateError):
            self.svc.rollback_stage("flag-a", self.subjects, set())
        # 消除偏离（当前 revision=9 为 0%，恢复为 50% 的确认值 revision=2）。
        self.svc.rollback(
            "flag-a", 2, self.subjects, self.impacted(0, 50))
        second = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))
        self.assertEqual(second["revision"], 10)
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(50, 100))
        self.assertEqual(result["restoredRevision"], 2)

    def test_history_and_later_stages_kept(self):
        self.advance_all()
        self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(50, 100))
        # 被回退阶段创建的 revision=3 仍可求值；后续阶段定义仍在计划中。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=3)["revision"],
            3)
        forecast = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        self.assertEqual([s["name"] for s in forecast["stages"]], ["full"])
        # 历史 revision 不被复用：重推末阶段分配 revision=4。
        again = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))
        self.assertEqual(again["revision"], 4)
        self.assertTrue(again["completed"])

    def test_duplicate_subject_ids_deduped_and_str_sorted(self):
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        flipped = sorted(self.impacted(0, 50), key=str)
        subjects = ([{"subject_id": sid} for sid in flipped[:2]]
                    + [{"subject_id": flipped[0]}])
        result = self.svc.rollback_stage(
            "flag-a", subjects, set(flipped[:2]))
        self.assertEqual(result["impacted"], flipped[:2])

    def test_conflict_carries_sorted_impact_and_changes_nothing(self):
        self.advance_all()
        expected = self.impacted(50, 100)
        with self.assertRaises(RolloutStageConflictError) as cm:
            self.svc.rollback_stage("flag-a", self.subjects, set())
        self.assertEqual(cm.exception.impacted, sorted(expected, key=str))
        self.assertEqual(cm.exception.args[1], sorted(expected, key=str))
        # 不改当前 revision、cursor、confirmed：正确影响面可重试成功。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 3)
        result = self.svc.rollback_stage("flag-a", self.subjects, expected)
        self.assertEqual(result["restoredRevision"], 2)
        self.assertEqual(result["cursor"], 1)

    def test_no_plan_cursor_zero_or_deviation_raises_state_error(self):
        svc = FeatureFlagService()
        svc.publish("flag-a", 1, make_definition())
        subjects = [{"subject_id": "u1"}]
        # 无计划。
        with self.assertRaises(RolloutStageStateError):
            svc.rollback_stage("flag-a", subjects, set())

        svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 100}])
        # cursor 为 0：尚未确认任何阶段。
        with self.assertRaises(RolloutStageStateError):
            svc.rollback_stage("flag-a", subjects, set())

        svc.advance_rollout_plan("flag-a", subjects, {"u1"})
        # rollback 使当前 revision 偏离最近确认值。
        svc.rollback("flag-a", 1, subjects, {"u1"})
        with self.assertRaises(RolloutStageStateError):
            svc.rollback_stage("flag-a", subjects, set())

        # publish 新版本同样构成偏离。
        svc.publish("flag-a", 5, make_definition())
        with self.assertRaises(RolloutStageStateError):
            svc.rollback_stage("flag-a", subjects, set())
        self.assertEqual(
            svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 5)

    def test_state_error_changes_nothing(self):
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        # promote_rollout 把当前 revision(50%) 推进到 100%，偏离计划确认值 2。
        self.svc.promote_rollout(
            "flag-a", 100, self.subjects, self.impacted(50, 100))
        with self.assertRaises(RolloutStageStateError):
            self.svc.rollback_stage("flag-a", self.subjects, set())
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 3)
        # 偏离消除后原计划仍可回退。
        self.svc.rollback(
            "flag-a", 2, self.subjects, self.impacted(50, 100))
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["restoredRevision"], 1)

    def test_unknown_flag_raises_not_found(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.rollback_stage("nope", [], set())

    def test_invalid_subjects_or_expected(self):
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
        # 参数错误后状态不变：当前仍是 revision=2，计划仍在。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["restoredRevision"], 1)

    def test_missing_subject_when_entering_rollout_branch(self):
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        with self.assertRaises(MissingSubjectError):
            self.svc.rollback_stage("flag-a", [{}], set())
        with self.assertRaises(MissingSubjectError):
            self.svc.rollback_stage("flag-a", [{"subject_id": ""}], set())
        # 异常后不改版本、不回退游标。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        result = self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["cursor"], 0)
        self.assertEqual(result["restoredRevision"], 1)

    def test_prerequisite_errors_propagate_and_change_nothing(self):
        svc = FeatureFlagService()
        # 依赖版本不存在：dep 已发布但 revision=7 不存在；空主体推进可成功，
        # 回退求值才暴露 RevisionNotFoundError。
        svc.publish("dep", 1, make_definition())
        svc.publish("flag-p", 1, make_definition(
            prerequisites=[{"flagKey": "dep", "revision": 7}]))
        svc.create_rollout_plan("flag-p", [{"name": "a", "percentage": 100}])
        svc.advance_rollout_plan("flag-p", [], set())
        with self.assertRaises(RevisionNotFoundError):
            svc.rollback_stage("flag-p", [{"subject_id": "u1"}], set())
        # 状态不变：游标仍是 1，空主体（不求值依赖，影响面为空）可成功回退。
        result = svc.rollback_stage("flag-p", [], set())
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["cursor"], 0)

        # 依赖成环。
        svc.publish("flag-c", 1, make_definition(
            prerequisites=[{"flagKey": "flag-c"}]))
        svc.create_rollout_plan("flag-c", [{"name": "a", "percentage": 100}])
        svc.advance_rollout_plan("flag-c", [], set())
        with self.assertRaises(PrerequisiteCycleError):
            svc.rollback_stage("flag-c", [{"subject_id": "u1"}], set())
        # 状态不变：空主体重试成功，游标回退为 0。
        result = svc.rollback_stage("flag-c", [], set())
        self.assertEqual(result["cursor"], 0)
        self.assertEqual(result["restoredRevision"], 1)

        # 依赖 flag 未发布。
        svc.publish("flag-m", 1, make_definition(
            prerequisites=[{"flagKey": "ghost"}]))
        svc.create_rollout_plan("flag-m", [{"name": "a", "percentage": 100}])
        svc.advance_rollout_plan("flag-m", [], set())
        with self.assertRaises(FlagNotFoundError):
            svc.rollback_stage("flag-m", [{"subject_id": "u1"}], set())

    def test_entrypoints_unchanged_after_stage_rollback(self):
        self.advance_all()
        self.svc.rollback_stage(
            "flag-a", self.subjects, self.impacted(50, 100))
        # 回退后计划未完成：重复登记仍冲突，推进/预演口径不变。
        from flag_rollout import RolloutPlanConflictError
        with self.assertRaises(RolloutPlanConflictError):
            self.svc.create_rollout_plan("flag-a", [
                {"name": "x", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))
        # 重新完成后可登记新计划（先回滚到 50% 以下才有更高比例可登记）。
        self.svc.rollback(
            "flag-a", 1, self.subjects, {s["subject_id"] for s in self.subjects})
        plan = self.svc.create_rollout_plan("flag-a", [
            {"name": "again", "percentage": 100}])
        self.assertEqual(plan["baseRevision"], 1)


if __name__ == "__main__":
    unittest.main()
