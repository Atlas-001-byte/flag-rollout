"""forecast_rollout_plan（只读计划预演）的单元测试。"""

import hashlib
import unittest

from flag_rollout import (
    FeatureFlagService,
    FlagNotFoundError,
    InvalidRolloutChangeError,
    MissingSubjectError,
    RevisionNotFoundError,
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


class ForecastRolloutPlanTest(unittest.TestCase):
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

    def create_plan(self, stages=(50, 100)):
        return self.svc.create_rollout_plan("flag-a", [
            {"name": "s%d" % i, "percentage": pct} for i, pct in enumerate(stages)
        ])

    def test_forecast_reports_remaining_stages_in_order(self):
        self.create_plan()
        result = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        self.assertEqual(result["flagKey"], "flag-a")
        self.assertEqual(result["baseRevision"], 1)
        self.assertEqual(result["currentRevision"], 1)
        self.assertEqual([s["name"] for s in result["stages"]], ["s0", "s1"])
        self.assertEqual(
            [s["index"] for s in result["stages"]], [0, 1])
        self.assertEqual(
            [s["percentage"] for s in result["stages"]], [50, 100])
        # forecast_revision 是连续推进将使用的第一版本号及后续连续版本号。
        self.assertEqual(
            [s["forecast_revision"] for s in result["stages"]], [2, 3])

        first, second = result["stages"]
        # 首阶段对比当前 revision，后续对比上一模拟阶段。
        self.assertEqual(first["impacted"], sorted(self.impacted(0, 50), key=str))
        self.assertEqual(second["impacted"], sorted(self.impacted(50, 100), key=str))
        # cumulativeImpacted 是到该阶段的累计变化并集。
        self.assertEqual(
            first["cumulativeImpacted"], sorted(self.impacted(0, 50), key=str))
        self.assertEqual(
            second["cumulativeImpacted"],
            sorted(self.impacted(0, 50) | self.impacted(50, 100), key=str))
        # totalImpacted 是剩余阶段变化的并集。
        self.assertEqual(
            result["totalImpacted"],
            sorted(self.impacted(0, 50) | self.impacted(50, 100), key=str))

    def test_forecast_is_read_only_and_repeatable(self):
        self.create_plan()
        first = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        second = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        self.assertEqual(first, second)
        # 不创建 revision、不改 current：当前仍是 revision=1，且没有 revision=2。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        with self.assertRaises(RevisionNotFoundError):
            self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=2)
        # 不推进计划：随后推进仍从第一阶段开始，版本号与 forecast 一致。
        advanced = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        self.assertEqual(advanced["revision"], 2)
        self.assertEqual(advanced["stage"]["index"], 0)
        self.assertEqual(advanced["impacted"], first["stages"][0]["impacted"])

    def test_forecast_after_partial_advance_uses_confirmed_revision(self):
        self.create_plan()
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        result = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        self.assertEqual(result["baseRevision"], 1)
        self.assertEqual(result["currentRevision"], 2)
        # 只剩游标后的阶段，forecast_revision 接续已建版本。
        self.assertEqual(len(result["stages"]), 1)
        stage = result["stages"][0]
        self.assertEqual(stage["name"], "s1")
        self.assertEqual(stage["index"], 1)
        self.assertEqual(stage["forecast_revision"], 3)
        # 以最近确认 revision（50%）为底稿，首阶段对比当前 revision。
        self.assertEqual(stage["impacted"], sorted(self.impacted(50, 100), key=str))
        self.assertEqual(
            result["totalImpacted"], sorted(self.impacted(50, 100), key=str))

    def test_forecast_revision_follows_max_revision_with_gaps(self):
        self.svc.publish("flag-a", 7, make_definition())
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        result = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        self.assertEqual(result["currentRevision"], 7)
        self.assertEqual(
            [s["forecast_revision"] for s in result["stages"]], [8, 9])

    def test_candidate_keeps_rules_enabled_and_bucket_order(self):
        self.svc.publish("flag-r", 1, make_definition(
            rules=[{"attribute": "vip", "operator": "equals",
                    "value": True, "serve": True}]))
        self.svc.create_rollout_plan("flag-r", [{"name": "a", "percentage": 100}])
        subjects = [{"subject_id": "u1", "vip": True}, {"subject_id": "u2"}]
        result = self.svc.forecast_rollout_plan("flag-r", subjects)
        # 规则命中主体不进入放量，只有 u2 因放量翻转。
        self.assertEqual(result["stages"][0]["impacted"], ["u2"])
        self.assertEqual(result["totalImpacted"], ["u2"])

    def test_duplicate_subject_ids_deduped_and_str_sorted(self):
        self.create_plan((100,))
        subjects = [{"subject_id": "u10"}, {"subject_id": "u2"},
                    {"subject_id": "u2"}, {"subject_id": "u1"}]
        result = self.svc.forecast_rollout_plan("flag-a", subjects)
        self.assertEqual(result["stages"][0]["impacted"], ["u1", "u10", "u2"])
        self.assertEqual(result["totalImpacted"], ["u1", "u10", "u2"])

    def test_unknown_flag_raises_not_found(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.forecast_rollout_plan("nope", [])

    def test_no_plan_completed_or_deviation_raises_state_error(self):
        # 无计划。
        with self.assertRaises(RolloutPlanStateError):
            self.svc.forecast_rollout_plan("flag-a", self.subjects)

        self.create_plan()
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.impacted(0, 50))
        # rollback 使当前 revision 偏离最近确认值。
        self.svc.rollback("flag-a", 1, self.subjects, self.impacted(0, 50))
        with self.assertRaises(RolloutPlanStateError):
            self.svc.forecast_rollout_plan("flag-a", self.subjects)

        # 计划完成后不可预演。
        svc2 = FeatureFlagService()
        svc2.publish("f", 1, make_definition())
        svc2.create_rollout_plan("f", [{"name": "a", "percentage": 100}])
        svc2.advance_rollout_plan("f", [{"subject_id": "u1"}], {"u1"})
        with self.assertRaises(RolloutPlanStateError):
            svc2.forecast_rollout_plan("f", [{"subject_id": "u1"}])

    def test_invalid_subjects(self):
        self.create_plan((100,))
        # subjects 不可迭代。
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.forecast_rollout_plan("flag-a", 42)
        # 上下文不是 Mapping。
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.forecast_rollout_plan("flag-a", [42])
        # subject_id 不可哈希且在 100% 下必然翻转。
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.forecast_rollout_plan("flag-a", [{"subject_id": ["u1"]}])
        # 参数错误后状态不变：当前仍是 revision=1，计划仍可预演、可推进。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        result = self.svc.forecast_rollout_plan(
            "flag-a", [{"subject_id": "u1"}])
        self.assertEqual(result["stages"][0]["impacted"], ["u1"])
        advanced = self.svc.advance_rollout_plan(
            "flag-a", [{"subject_id": "u1"}], {"u1"})
        self.assertEqual(advanced["revision"], 2)

    def test_missing_subject_when_entering_rollout_branch(self):
        self.create_plan((100,))
        with self.assertRaises(MissingSubjectError):
            self.svc.forecast_rollout_plan("flag-a", [{}])
        with self.assertRaises(MissingSubjectError):
            self.svc.forecast_rollout_plan("flag-a", [{"subject_id": ""}])
        # 异常后不建版本、不推进。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        result = self.svc.forecast_rollout_plan(
            "flag-a", [{"subject_id": "u1"}])
        self.assertEqual(result["stages"][0]["forecast_revision"], 2)

    def test_state_error_changes_nothing(self):
        self.create_plan()
        # promote_rollout 把当前 revision 推进到 2，偏离计划最近确认值 1。
        self.svc.promote_rollout(
            "flag-a", 100, self.subjects,
            {s["subject_id"] for s in self.subjects})
        with self.assertRaises(RolloutPlanStateError):
            self.svc.forecast_rollout_plan("flag-a", self.subjects)
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 2)


if __name__ == "__main__":
    unittest.main()
