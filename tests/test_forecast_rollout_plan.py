"""只读影响面预演（forecast_rollout_plan）的单元测试。"""

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

    def band(self, old_pct, new_pct, flag_key="flag-a"):
        return {
            s["subject_id"] for s in self.subjects
            if old_pct * 100
            <= bucket_of(flag_key, "s1", s["subject_id"])
            < new_pct * 100
        }

    def test_returns_remaining_stages_with_revisions_and_impacts(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 10},
            {"name": "beta", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        result = self.svc.forecast_rollout_plan("flag-a", self.subjects)

        self.assertEqual(result["flagKey"], "flag-a")
        self.assertEqual(result["baseRevision"], 1)
        self.assertEqual(result["currentRevision"], 1)
        self.assertEqual(
            [s["name"] for s in result["stages"]],
            ["canary", "beta", "full"],
        )
        self.assertEqual(
            [(s["percentage"], s["index"], s["forecast_revision"])
             for s in result["stages"]],
            [(10, 0, 2), (50, 1, 3), (100, 2, 4)],
        )

        canary, beta, full = result["stages"]
        self.assertEqual(canary["impacted"],
                         sorted(self.band(0, 10), key=str))
        self.assertEqual(canary["cumulativeImpacted"],
                         sorted(self.band(0, 10), key=str))
        self.assertEqual(beta["impacted"],
                         sorted(self.band(10, 50), key=str))
        self.assertEqual(beta["cumulativeImpacted"],
                         sorted(self.band(0, 50), key=str))
        self.assertEqual(full["impacted"],
                         sorted(self.band(50, 100), key=str))
        self.assertEqual(full["cumulativeImpacted"],
                         sorted(self.band(0, 100), key=str))
        # totalImpacted 是剩余阶段变化并集，等于最后一阶段的累计变化。
        self.assertEqual(result["totalImpacted"],
                         sorted(self.band(0, 100), key=str))
        self.assertEqual(result["totalImpacted"], full["cumulativeImpacted"])
        # 各阶段增量互不相交，且并集等于 totalImpacted。
        union = set(canary["impacted"]) | set(beta["impacted"]) | set(full["impacted"])
        self.assertEqual(union, set(result["totalImpacted"]))

    def test_first_stage_impact_matches_actual_advance(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50},
            {"name": "b", "percentage": 100},
        ])
        forecast = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        advanced = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, set(forecast["stages"][0]["impacted"]))
        self.assertEqual(advanced["revision"],
                         forecast["stages"][0]["forecast_revision"])
        self.assertEqual(advanced["impacted"], forecast["stages"][0]["impacted"])

    def test_forecast_after_partial_advance_starts_at_cursor(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "canary", "percentage": 10},
            {"name": "beta", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.band(0, 10))

        result = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        self.assertEqual(result["baseRevision"], 1)
        self.assertEqual(result["currentRevision"], 2)
        self.assertEqual(
            [(s["name"], s["index"], s["forecast_revision"])
             for s in result["stages"]],
            [("beta", 1, 3), ("full", 2, 4)],
        )
        beta, full = result["stages"]
        # 首项对当前 revision（已在 10%）比较，后续对上一模拟阶段。
        self.assertEqual(beta["impacted"], sorted(self.band(10, 50), key=str))
        self.assertEqual(beta["cumulativeImpacted"],
                         sorted(self.band(10, 50), key=str))
        self.assertEqual(full["impacted"], sorted(self.band(50, 100), key=str))
        self.assertEqual(full["cumulativeImpacted"],
                         sorted(self.band(10, 100), key=str))
        self.assertEqual(result["totalImpacted"],
                         sorted(self.band(10, 100), key=str))

    def test_forecast_then_all_advances_use_forecast_revisions_in_order(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 10},
            {"name": "b", "percentage": 50},
            {"name": "c", "percentage": 100},
        ])
        forecast = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        for stage in forecast["stages"]:
            advanced = self.svc.advance_rollout_plan(
                "flag-a", self.subjects, set(stage["impacted"]))
            self.assertEqual(advanced["revision"], stage["forecast_revision"])
            self.assertEqual(advanced["stage"]["name"], stage["name"])
            self.assertEqual(advanced["impacted"], stage["impacted"])
        # 计划完成后没有剩余阶段可预演。
        with self.assertRaises(RolloutPlanStateError):
            self.svc.forecast_rollout_plan("flag-a", self.subjects)

    def test_forecast_revision_starts_from_max_plus_one_with_gaps(self):
        self.svc.publish("flag-a", 3, make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True}))
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        result = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        self.assertEqual(result["currentRevision"], 3)
        self.assertEqual([s["forecast_revision"] for s in result["stages"]], [4, 5])

    def test_rules_enabled_and_bucket_order_are_reused(self):
        # enabled=False：放量分支不可达，任何阶段都不产生影响。
        self.svc.publish("flag-off", 1, make_definition(enabled=False))
        self.svc.create_rollout_plan("flag-off", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        result = self.svc.forecast_rollout_plan(
            "flag-off", [{"subject_id": "u1"}, {"subject_id": "u2"}])
        self.assertEqual(result["totalImpacted"], [])
        self.assertTrue(all(s["impacted"] == [] for s in result["stages"]))

        # 规则优先于放量：命中规则的主体在所有阶段都不翻转。
        self.svc.publish("flag-r", 1, make_definition(
            rules=[{"attribute": "vip", "operator": "equals",
                    "value": True, "serve": True}]))
        self.svc.create_rollout_plan("flag-r", [
            {"name": "a", "percentage": 100}])
        result = self.svc.forecast_rollout_plan(
            "flag-r", [{"subject_id": "u1", "vip": True}, {"subject_id": "u2"}])
        self.assertEqual(result["stages"][0]["impacted"], ["u2"])
        # 桶仍按 flag_key:salt:subject_id 计算，salt 沿用底稿。
        for entry in result["stages"]:
            self.assertEqual(entry["impacted"], ["u2"])

    def test_duplicate_subject_ids_deduped_and_str_sorted(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        subjects = [{"subject_id": "u10"}, {"subject_id": "u2"},
                    {"subject_id": "u2"}, {"subject_id": "u1"}]
        result = self.svc.forecast_rollout_plan("flag-a", subjects)
        self.assertEqual(result["totalImpacted"], ["u1", "u10", "u2"])
        for stage in result["stages"]:
            self.assertEqual(stage["impacted"], sorted(stage["impacted"], key=str))

    def test_accepts_any_iterable_and_is_deterministic(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        first = self.svc.forecast_rollout_plan(
            "flag-a", ({"subject_id": s["subject_id"]} for s in self.subjects))
        second = self.svc.forecast_rollout_plan("flag-a", list(self.subjects))
        self.assertEqual(first, second)

    def test_forecast_is_read_only(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.forecast_rollout_plan("flag-a", self.subjects)
        # 不创建 revision：当前仍为 1，revision=2 不存在。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        with self.assertRaises(RevisionNotFoundError):
            self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=2)
        # cursor 未动：再次预演仍是全部阶段；计划可按原始影响面推进。
        again = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        self.assertEqual([s["index"] for s in again["stages"]], [0, 1])
        advanced = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.band(0, 50))
        self.assertEqual(advanced["revision"], 2)
        self.assertEqual(advanced["stage"]["index"], 0)

    def test_no_plan_completed_or_deviation_raises_state_error(self):
        # 无计划。
        with self.assertRaises(RolloutPlanStateError):
            self.svc.forecast_rollout_plan("flag-a", self.subjects)

        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.band(0, 50))
        # rollback 使当前 revision 偏离最近确认值。
        self.svc.rollback(
            "flag-a", 1, self.subjects, self.band(0, 50))
        with self.assertRaises(RolloutPlanStateError):
            self.svc.forecast_rollout_plan("flag-a", self.subjects)

        # publish 新版本同样构成偏离。
        self.svc.publish("flag-a", 5, make_definition())
        with self.assertRaises(RolloutPlanStateError):
            self.svc.forecast_rollout_plan("flag-a", self.subjects)

        # 计划完成后再预演。
        svc = FeatureFlagService()
        svc.publish("f", 1, make_definition())
        svc.create_rollout_plan("f", [{"name": "a", "percentage": 100}])
        svc.advance_rollout_plan("f", [{"subject_id": "u1"}], {"u1"})
        with self.assertRaises(RolloutPlanStateError):
            svc.forecast_rollout_plan("f", [{"subject_id": "u1"}])

    def test_unknown_flag_raises_not_found(self):
        with self.assertRaises(FlagNotFoundError):
            self.svc.forecast_rollout_plan("nope", self.subjects)

    def test_invalid_subjects(self):
        self.svc.create_rollout_plan("flag-a", [{"name": "a", "percentage": 100}])
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.forecast_rollout_plan("flag-a", 42)
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.forecast_rollout_plan("flag-a", [42])
        # subject_id 不可哈希且在 100% 下必然进入放量翻转。
        with self.assertRaises(InvalidRolloutChangeError):
            self.svc.forecast_rollout_plan(
                "flag-a", [{"subject_id": ["u1"]}])
        # 参数错误后状态不变。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        result = self.svc.forecast_rollout_plan(
            "flag-a", [{"subject_id": "u1"}])
        self.assertEqual(result["stages"][0]["impacted"], ["u1"])

    def test_missing_subject_when_entering_rollout_branch(self):
        self.svc.create_rollout_plan("flag-a", [{"name": "a", "percentage": 100}])
        with self.assertRaises(MissingSubjectError):
            self.svc.forecast_rollout_plan("flag-a", [{}])
        with self.assertRaises(MissingSubjectError):
            self.svc.forecast_rollout_plan("flag-a", [{"subject_id": ""}])
        with self.assertRaises(MissingSubjectError):
            self.svc.forecast_rollout_plan(
                "flag-a", [{"subject_id": "u1"}, {}])
        # 异常后不建版本、不推进。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)
        result = self.svc.forecast_rollout_plan(
            "flag-a", [{"subject_id": "u1"}])
        self.assertEqual([s["index"] for s in result["stages"]], [0])

    def test_errors_keep_state(self):
        self.svc.create_rollout_plan("flag-a", [
            {"name": "a", "percentage": 50}, {"name": "b", "percentage": 100}])
        for bad_subjects in (42, [42], [{}], [{"subject_id": ["u"]}]):
            with self.assertRaises(Exception):
                self.svc.forecast_rollout_plan("flag-a", bad_subjects)
        # 全部异常后仍可正常预演与推进，且修订号从 2 开始。
        forecast = self.svc.forecast_rollout_plan("flag-a", self.subjects)
        self.assertEqual(forecast["stages"][0]["forecast_revision"], 2)
        advanced = self.svc.advance_rollout_plan(
            "flag-a", self.subjects, self.band(0, 50))
        self.assertEqual(advanced["revision"], 2)


if __name__ == "__main__":
    unittest.main()
