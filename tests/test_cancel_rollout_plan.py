"""取消放量计划（cancel_rollout_plan）的单元测试。"""

import hashlib
import unittest

from flag_rollout import (
    FeatureFlagService,
    FlagNotFoundError,
    InvalidRolloutChangeError,
    MissingSubjectError,
    RolloutCancelConflictError,
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

    def test_cancel_before_any_advance(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        # 未推进：当前即基准，影响面为空，cancelledStage 为 None。
        result = self.svc.cancel_rollout_plan("flag-a", self.subjects, set())
        self.assertEqual(result["flagKey"], "flag-a")
        self.assertEqual(result["restoredRevision"], 1)
        self.assertIsNone(result["cancelledStage"])
        self.assertEqual(result["impacted"], [])
        # 计划已删除，可立即新建计划。
        new_plan = self.svc.create_rollout_plan("flag-a", [
            {"name": "again", "percentage": 100}])
        self.assertEqual(new_plan["baseRevision"], 1)
        self.assertEqual(new_plan["stages"][0]["name"], "again")

    def test_cancel_after_advance_restores_base_revision(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        expected = self.impacted(0, 50)
        result = self.svc.cancel_rollout_plan("flag-a", self.subjects, expected)
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["cancelledStage"], "canary")
        self.assertEqual(result["impacted"], sorted(expected, key=str))
        # 当前 revision 回到基准；推进产生的历史 revision 仍可按 revision 求值。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        historical = self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=2)
        self.assertEqual(historical["revision"], 2)

    def test_cancelled_stage_is_latest_confirmed_stage(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(50, 100))
        # 计划已完成，不可取消。
        with self.assertRaises(RolloutPlanStateError):
            self.svc.cancel_rollout_plan(
                "flag-a", self.subjects, self.impacted(0, 100))

        svc = FeatureFlagService()
        svc.publish("f", 1, make_definition())
        svc.create_rollout_plan("f", [
            {"name": "s1", "percentage": 50},
            {"name": "s2", "percentage": 90},
            {"name": "s3", "percentage": 100},
        ])
        subjects = self.subjects

        def impacted(old_pct, new_pct):
            return {
                s["subject_id"] for s in subjects
                if old_pct * 100 <= bucket_of("f", "s1", s["subject_id"]) < new_pct * 100
            }

        svc.advance_rollout_plan("f", subjects, impacted(0, 50))
        svc.advance_rollout_plan("f", subjects, impacted(50, 90))
        result = svc.cancel_rollout_plan("f", subjects, impacted(0, 90))
        self.assertEqual(result["cancelledStage"], "s2")
        self.assertEqual(result["restoredRevision"], 1)

    def test_duplicate_subject_ids_deduped_and_str_sorted(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        flipped = sorted(self.impacted(0, 50), key=str)
        subjects = ([{"subject_id": sid} for sid in flipped[:2]]
                    + [{"subject_id": flipped[0]}])
        result = self.svc.cancel_rollout_plan(
            "flag-a", subjects, set(flipped[:2]))
        self.assertEqual(result["impacted"], flipped[:2])

    def test_conflict_carries_sorted_impact_and_changes_nothing(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        with self.assertRaises(RolloutCancelConflictError) as cm:
            self.svc.cancel_rollout_plan("flag-a", self.subjects, set())
        self.assertEqual(cm.exception.impacted,
                         sorted(self.impacted(0, 50), key=str))
        self.assertEqual(cm.exception.args[1],
                         sorted(self.impacted(0, 50), key=str))
        # 不改当前 revision、不删计划：正确影响面可重试成功。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        result = self.svc.cancel_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["restoredRevision"], 1)
        self.assertEqual(result["cancelledStage"], "a")

    def test_no_plan_completed_or_deviation_raises_state_error(self):
        # 无计划。
        with self.assertRaises(RolloutPlanStateError):
            self.svc.cancel_rollout_plan("flag-a", [], set())

        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        # rollback 使当前 revision 偏离最近确认值。
        self.svc.rollback("flag-a", 1, self.subjects, self.impacted(0, 50))
        with self.assertRaises(RolloutPlanStateError):
            self.svc.cancel_rollout_plan("flag-a", self.subjects, set())

        # publish 新版本同样构成偏离。
        self.svc.publish("flag-a", 5, make_definition())
        with self.assertRaises(RolloutPlanStateError):
            self.svc.cancel_rollout_plan("flag-a", self.subjects, set())

        # 状态错误不改版本、不删计划：偏离消除前不可取消，计划仍存在。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 5)

    def test_state_error_changes_nothing(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        # promote_rollout 把当前 revision 推进到 2，偏离计划最近确认值 1。
        self.svc.promote_rollout(
            "flag-a", 100, self.subjects,
            {s["subject_id"] for s in self.subjects})
        with self.assertRaises(RolloutPlanStateError):
            self.svc.cancel_rollout_plan("flag-a", self.subjects, set())
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        # 状态错误不删计划：rollback 消除偏离后，原计划仍可取消。
        self.svc.rollback(
            "flag-a", 1, self.subjects, {s["subject_id"] for s in self.subjects})
        result = self.svc.cancel_rollout_plan("flag-a", self.subjects, set())
        self.assertEqual(result["restoredRevision"], 1)
        self.assertIsNone(result["cancelledStage"])

    def test_unknown_flag_raises_not_found(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.cancel_rollout_plan("nope", [], set())

    def test_invalid_subjects_or_expected(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan("flag-a", 42, set())
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan("flag-a", self.subjects, 42)
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan("flag-a", self.subjects, [["u1"]])
        # 上下文非 Mapping。
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan("flag-a", [42], set())
        # subject_id 不可哈希且必然翻转（取一个 50% 下命中的不可哈希 id）。
        unhashable = None
        for i in range(100):
            candidate = ["u%d" % i]
            if bucket_of("flag-a", "s1", candidate) < 5000:
                unhashable = candidate
                break
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.cancel_rollout_plan("flag-a", [{"subject_id": unhashable}], set())
        # 参数错误后状态不变：当前仍是推进后的 revision=2，计划仍在。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        result = self.svc.cancel_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(result["restoredRevision"], 1)

    def test_missing_subject_when_entering_rollout_branch(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        with self.assertRaises(MissingSubjectError):
            self.svc.cancel_rollout_plan("flag-a", [{}], set())
        with self.assertRaises(MissingSubjectError):
            self.svc.cancel_rollout_plan("flag-a", [{"subject_id": ""}], set())
        # 异常后不改版本、不删计划。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)
        self.svc.cancel_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)

    def test_cancel_does_not_create_revision(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.cancel_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        # 取消只恢复基准 revision，不新建版本：revision=3 不存在。
        with self.assertRaises(Exception):
            self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=3)

    def test_existing_entrypoints_unchanged_after_cancel(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.svc.cancel_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        # 取消后 promote_rollout / rollback 行为与基线一致。
        result = self.svc.promote_rollout(
            "flag-a", 100, self.subjects,
            {s["subject_id"] for s in self.subjects})
        self.assertEqual(result["revision"], 3)
        impacted = self.svc.rollback(
            "flag-a", 1, self.subjects,
            {s["subject_id"] for s in self.subjects})
        self.assertEqual(len(impacted), len(self.subjects))
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)


if __name__ == "__main__":
    unittest.main()
