"""preview_change 灰度预演入口的单元测试，仅依赖标准库。"""

import copy
import hashlib
import unittest

from flag_rollout import (
    FeatureFlagService,
    FlagNotFoundError,
    PreviewValidationError,
)


def bucket_of(flag_key, salt, subject_id):
    digest = hashlib.sha256(("%s:%s:%s" % (flag_key, salt, subject_id)).encode("utf-8")).digest()
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


def find_overlap_subject(flag_key, salt, first_pct, second_pct):
    """在两个独立放量环中都命中的 subject（用于构造 stage_overlap）。"""
    for i in range(100000):
        subject = "ov-%d" % i
        if (
            bucket_of(flag_key, "%s#preview-stage-0" % salt, subject) < first_pct * 100
            and bucket_of(flag_key, "%s#preview-stage-1" % salt, subject)
            < second_pct * 100
        ):
            return subject
    raise AssertionError("未找到双阶段同时命中的主体")


class PreviewSuccessTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        # 现行版本：enabled，放量 0%，无人开启。
        self.svc.publish("flag-a", 1, make_definition())

    def _request(self, **overrides):
        request = {
            "flagKey": "flag-a",
            "definition": make_definition(),
            "stages": [{"percentage": 100, "name": "full"}],
            "contexts": [{"subjectKey": "u1"}, {"subjectKey": "u2"}],
        }
        request.update(overrides)
        return request

    def test_full_rollout_turns_everyone_on(self):
        response = self.svc.preview_change(self._request())
        self.assertEqual(response["flagKey"], "flag-a")
        self.assertEqual(response["currentRevision"], 1)
        self.assertEqual(
            response["results"],
            [
                {"subjectKey": "u1", "before": False, "after": True,
                 "beforeRuleId": None, "afterRuleId": None,
                 "changeReason": "off_to_on", "stage": "full"},
                {"subjectKey": "u2", "before": False, "after": True,
                 "beforeRuleId": None, "afterRuleId": None,
                 "changeReason": "off_to_on", "stage": "full"},
            ],
        )
        self.assertEqual(
            response["summary"],
            {"total": 2, "offToOn": 2, "onToOff": 0, "unchanged": 0,
             "stageHits": {"full": 2}, "affectedSubjects": ["u1", "u2"]},
        )
        for item in response["results"]:
            self.assertIsInstance(item["before"], bool)
            self.assertIsInstance(item["after"], bool)

    def test_on_to_off_and_unchanged(self):
        # 现行版本全量开启；候选阶段 0%（独立环无人命中）、默认关闭 → 由开变关。
        self.svc.publish("flag-a", 2, make_definition(
            rollout={"percentage": 100, "salt": "s1", "serve": True}))
        response = self.svc.preview_change(
            self._request(stages=[{"percentage": 0, "name": "off"}]))
        self.assertEqual(response["summary"]["onToOff"], 2)
        self.assertEqual(response["summary"]["offToOn"], 0)
        self.assertEqual(response["summary"]["affectedSubjects"], ["u1", "u2"])
        for item in response["results"]:
            self.assertEqual(item["changeReason"], "on_to_off")
            self.assertIsNone(item["stage"])

    def test_candidate_rule_hit_reports_rule_id_and_stage_none(self):
        candidate = make_definition(rules=[
            {"id": "r-pro", "attribute": "plan", "operator": "equals",
             "value": "pro", "serve": True},
        ])
        request = self._request(
            definition=candidate,
            stages=[{"percentage": 100}],
            contexts=[{"subjectKey": "u1", "plan": "pro"}, {"subjectKey": "u2"}],
        )
        response = self.svc.preview_change(request)
        first, second = response["results"]
        # 规则优先于放量：u1 命中规则，不属于任何阶段。
        self.assertEqual((first["after"], first["afterRuleId"], first["stage"]),
                         (True, "r-pro", None))
        self.assertEqual((second["afterRuleId"], second["stage"]), (None, "stage-0"))
        self.assertEqual(response["summary"]["stageHits"], {"stage-0": 1})

    def test_rule_id_defaults_to_zero_based_index(self):
        candidate = make_definition(rules=[
            {"attribute": "plan", "operator": "equals", "value": "pro", "serve": True},
            {"attribute": "vip", "operator": "equals", "value": True, "serve": True},
        ])
        request = self._request(
            definition=candidate,
            contexts=[{"subjectKey": "u1", "vip": True}, {"subjectKey": "u2", "plan": "pro"}],
        )
        response = self.svc.preview_change(request)
        self.assertEqual(response["results"][0]["afterRuleId"], 1)
        self.assertEqual(response["results"][1]["afterRuleId"], 0)

    def test_partial_stage_matches_bucket_semantics(self):
        subjects = ["u%d" % i for i in range(40)]
        request = self._request(
            stages=[{"name": "canary", "percentage": 25}],
            contexts=[{"subjectKey": s} for s in subjects],
        )
        response = self.svc.preview_change(request)
        expected_hit = {
            s for s in subjects
            if bucket_of("flag-a", "s1#preview-stage-0", s) < 2500
        }
        self.assertEqual(response["summary"]["stageHits"]["canary"], len(expected_hit))
        self.assertEqual(response["summary"]["offToOn"], len(expected_hit))
        hit_items = {item["subjectKey"] for item in response["results"]
                     if item["stage"] == "canary"}
        self.assertEqual(hit_items, expected_hit)

    def test_each_context_belongs_to_at_most_one_stage(self):
        # 独立放量环：挑选仅命中其中一个环的主体，验证阶段归属互斥且计数一致。
        only_a, only_b = [], []
        for i in range(100000):
            subject = "p-%d" % i
            hit_a = bucket_of("flag-a", "s1#preview-stage-0", subject) < 1000
            hit_b = bucket_of("flag-a", "s1#preview-stage-1", subject) < 5000
            if hit_a and not hit_b:
                only_a.append(subject)
            elif hit_b and not hit_a:
                only_b.append(subject)
            if len(only_a) >= 3 and len(only_b) >= 3:
                break
        subjects = only_a[:3] + only_b[:3]
        request = self._request(
            stages=[{"name": "canary", "percentage": 10},
                    {"name": "beta", "percentage": 50}],
            contexts=[{"subjectKey": s} for s in subjects],
        )
        response = self.svc.preview_change(request)
        owners = [(item["subjectKey"], item["stage"]) for item in response["results"]]
        self.assertEqual({s for s, _ in owners}, set(subjects))
        self.assertEqual(
            sorted(stage for _, stage in owners if stage),
            ["beta"] * 3 + ["canary"] * 3,
        )
        totals = response["summary"]["stageHits"]
        self.assertEqual(totals, {"canary": 3, "beta": 3})

    def test_truthy_non_bool_serve_normalized_to_bool(self):
        # 现行版本规则 serve "on"（真值字符串）→ before 归一化为 True；
        # 候选停用且默认 False → on_to_off，布尔归一化与影响面语义一致。
        self.svc.publish("flag-a", 2, make_definition(
            rules=[{"attribute": "x", "operator": "equals", "value": 1,
                    "serve": "on"}],
            rollout={"percentage": 0, "salt": "s1", "serve": True}))
        candidate = make_definition(enabled=False, default=False)
        request = self._request(definition=candidate,
                                contexts=[{"subjectKey": "u1", "x": 1},
                                          {"subjectKey": "u2"}])
        response = self.svc.preview_change(request)
        self.assertEqual(response["results"][0]["before"], True)
        self.assertEqual(response["results"][0]["changeReason"], "on_to_off")
        self.assertEqual(response["results"][1]["changeReason"], "unchanged")

    def test_disabled_candidate_never_hits_stage(self):
        candidate = make_definition(enabled=False, default=False)
        request = self._request(definition=candidate)
        response = self.svc.preview_change(request)
        self.assertEqual(response["summary"]["stageHits"], {"full": 0})
        self.assertTrue(all(item["changeReason"] == "unchanged"
                            for item in response["results"]))

    def test_affected_subjects_sorted_by_str(self):
        request = self._request(contexts=[
            {"subjectKey": "z"}, {"subjectKey": 10}, {"subjectKey": "a"},
            {"subjectKey": 2},
        ])
        response = self.svc.preview_change(request)
        self.assertEqual(
            response["summary"]["affectedSubjects"],
            sorted(["z", 10, "a", 2], key=str),
        )

    def test_preview_matches_direct_evaluation(self):
        # 现行：p10；候选阶段 100% 独立环。用另一个已发布 flag 承载候选阶段
        # 定义，逐项核对 before/after 与直接 evaluate 完全一致。
        self.svc.publish("flag-a", 2, make_definition(
            rollout={"percentage": 10, "salt": "s1", "serve": True}))
        self.svc.publish("flag-b", 1, make_definition(
            rollout={"percentage": 100, "salt": "s1#preview-stage-0", "serve": True}))
        subjects = ["u%d" % i for i in range(30)]
        response = self.svc.preview_change(self._request(
            stages=[{"name": "full", "percentage": 100}],
            contexts=[{"subjectKey": s} for s in subjects]))
        for item, subject in zip(response["results"], subjects):
            ctx = {"subject_id": subject}
            self.assertEqual(
                item["before"],
                bool(self.svc.evaluate("flag-a", ctx)["enabled"]),
            )
            self.assertEqual(
                item["after"],
                bool(self.svc.evaluate("flag-b", ctx)["enabled"]),
            )
            self.assertEqual(item["stage"] == "full",
                             self.svc.evaluate("flag-b", ctx)["reason"] == "rollout")

    def test_identical_current_and_candidate_yields_all_unchanged(self):
        # 现行 0%，候选阶段同为 0%（无人命中独立环）→ 逐项 unchanged。
        response = self.svc.preview_change(self._request(
            stages=[{"name": "zero", "percentage": 0}],
            contexts=[{"subjectKey": "u1"}, {"subjectKey": "u2"}]))
        self.assertEqual(response["summary"],
                         {"total": 2, "offToOn": 0, "onToOff": 0, "unchanged": 2,
                          "stageHits": {"zero": 0}, "affectedSubjects": []})
        self.assertTrue(all(item["changeReason"] == "unchanged"
                            and item["before"] is False and item["after"] is False
                            for item in response["results"]))


class PreviewDeterminismTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        self.svc.publish("flag-a", 1, make_definition())

    def test_repeated_calls_are_identical(self):
        request = {
            "flagKey": "flag-a",
            "definition": make_definition(rules=[
                {"id": "r1", "attribute": "plan", "operator": "in",
                 "value": ["pro"], "serve": True}]),
            "stages": [{"name": "canary", "percentage": 50}],
            "contexts": [{"subjectKey": "u%03d" % i,
                          "plan": "pro" if i % 3 == 0 else "free"}
                         for i in range(50)],
        }
        first = self.svc.preview_change(copy.deepcopy(request))
        second = self.svc.preview_change(copy.deepcopy(request))
        self.assertEqual(first, second)

    def test_results_independent_of_input_mutation(self):
        request = {
            "flagKey": "flag-a",
            "definition": make_definition(),
            "stages": [{"percentage": 25}],
            "contexts": [{"subjectKey": "u1"}],
        }
        before = self.svc.preview_change(copy.deepcopy(request))
        request["definition"]["rollout"]["percentage"] = 100
        request["stages"][0]["percentage"] = 90
        request["contexts"].append({"subjectKey": "u2"})
        after = self.svc.preview_change(request)
        # 被外部篡改后的入参照常求值；而用原始入参重放结果不变。
        self.assertEqual(after["summary"]["total"], 2)
        replay = self.svc.preview_change({
            "flagKey": "flag-a",
            "definition": make_definition(),
            "stages": [{"percentage": 25}],
            "contexts": [{"subjectKey": "u1"}],
        })
        self.assertEqual(before, replay)


class PreviewNoSideEffectsTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        self.svc.publish("flag-a", 1, make_definition(
            rollout={"percentage": 10, "salt": "s1", "serve": True}))

    def test_preview_does_not_change_state(self):
        request = {
            "flagKey": "flag-a",
            "definition": make_definition(
                rollout={"percentage": 100, "salt": "s1", "serve": True}),
            "stages": [{"percentage": 100}],
            "contexts": [{"subjectKey": "u1"}, {"subjectKey": "u2"}],
        }
        before_eval = self.svc.evaluate("flag-a", {"subject_id": "u1"})
        self.svc.preview_change(request)
        # 多次预演后当前版本与现行决策不变、没有产生新 revision。
        self.svc.preview_change(copy.deepcopy(request))
        after_eval = self.svc.evaluate("flag-a", {"subject_id": "u1"})
        self.assertEqual(before_eval, after_eval)
        self.assertEqual(after_eval["revision"], 1)
        with self.assertRaises(KeyError):
            self.svc._flags["flag-a"].revisions[2]
        # 候选全量开启的结论只存在于预演结果中。
        self.assertEqual(
            self.svc.evaluate("flag-a", {"subject_id": "u1"}, revision=1)["revision"], 1
        )

    def test_failed_preview_does_not_change_state(self):
        good = {
            "flagKey": "flag-a",
            "definition": make_definition(),
            "stages": [{"percentage": 100}],
            "contexts": [{"subjectKey": "u1"}],
        }
        bad = copy.deepcopy(good)
        bad["stages"] = [{"percentage": 50}, {"percentage": 10}]
        with self.assertRaises(PreviewValidationError):
            self.svc.preview_change(bad)
        response = self.svc.preview_change(good)
        self.assertEqual(response["currentRevision"], 1)
        self.assertEqual(self.svc.evaluate("flag-a", {"subject_id": "u1"})["revision"], 1)


class PreviewValidationTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        self.svc.publish("flag-a", 1, make_definition())

    def _request(self, **overrides):
        request = {
            "flagKey": "flag-a",
            "definition": make_definition(),
            "stages": [{"percentage": 100}],
            "contexts": [{"subjectKey": "u1"}],
        }
        request.update(overrides)
        return request

    def assertErrorCode(self, code, request):
        with self.assertRaises(PreviewValidationError) as cm:
            self.svc.preview_change(request)
        self.assertEqual(cm.exception.error_code, code)

    def test_error_codes(self):
        cases = [
            ("invalid_payload", ["not", "an", "object"]),
            ("invalid_payload", None),
            ("flag_key_empty", self._request(flagKey="")),
            ("flag_key_empty", self._request(flagKey=123)),
            ("contexts_not_list", self._request(contexts={"u1": {}})),
            ("contexts_empty", self._request(contexts=[])),
            ("context_not_object", self._request(contexts=["u1"])),
            ("subject_key_missing", self._request(contexts=[{}])),
            ("subject_key_missing", self._request(contexts=[{"subjectKey": ""}])),
            ("subject_key_duplicate",
             self._request(contexts=[{"subjectKey": "u1"}, {"subjectKey": "u1"}])),
            ("stages_not_list", self._request(stages={"percentage": 1})),
            ("stages_empty", self._request(stages=[])),
            ("stage_not_object", self._request(stages=[50])),
            ("stage_name_invalid", self._request(stages=[{"percentage": 1, "name": 5}])),
            ("stage_name_duplicate",
             self._request(stages=[{"percentage": 1, "name": "s"},
                                   {"percentage": 2, "name": "s"}])),
            ("stage_percentages_not_non_decreasing",
             self._request(stages=[{"percentage": 50}, {"percentage": 10}])),
            ("candidate_not_evaluable",
             self._request(definition={"enabled": True})),
            ("candidate_not_evaluable",
             self._request(definition=make_definition(enabled="yes"))),
        ]
        for code, request in cases:
            with self.subTest(code=code):
                self.assertErrorCode(code, request)

    def test_percentage_invalid_codes(self):
        for bad in (-1, 101, 100.01, "50", True, False, None, float("nan")):
            with self.subTest(bad=bad):
                self.assertErrorCode(
                    "stage_percentage_invalid",
                    self._request(stages=[{"percentage": bad}]),
                )
        self.assertErrorCode(
            "stage_percentage_invalid", self._request(stages=[{"name": "s"}])
        )

    def test_boundary_percentages_are_valid(self):
        for pct in (0, 100, 0.0, 100.0):
            response = self.svc.preview_change(
                self._request(stages=[{"percentage": pct}]))
            self.assertEqual(response["summary"]["total"], 1)

    def test_equal_percentages_are_non_decreasing(self):
        # 50/50 相等合法；挑选在两个独立环中不同时命中的主体避免 overlap。
        subjects = []
        for i in range(100000):
            subject = "e-%d" % i
            hit_a = bucket_of("flag-a", "s1#preview-stage-0", subject) < 5000
            hit_b = bucket_of("flag-a", "s1#preview-stage-1", subject) < 5000
            if not (hit_a and hit_b):
                subjects.append(subject)
            if len(subjects) >= 4:
                break
        response = self.svc.preview_change(
            self._request(stages=[{"percentage": 50, "name": "a"},
                                  {"percentage": 50, "name": "b"}],
                          contexts=[{"subjectKey": s} for s in subjects]))
        self.assertEqual(set(response["summary"]["stageHits"]), {"a", "b"})

    def test_stage_overlap(self):
        subject = find_overlap_subject("flag-a", "s1", 50, 100)
        request = self._request(
            stages=[{"percentage": 50, "name": "a"},
                    {"percentage": 100, "name": "b"}],
            contexts=[{"subjectKey": subject}],
        )
        with self.assertRaises(PreviewValidationError) as cm:
            self.svc.preview_change(request)
        self.assertEqual(cm.exception.error_code, "stage_overlap")
        self.assertEqual(cm.exception.details["subjectKey"], subject)
        self.assertEqual(cm.exception.details["stages"], ["a", "b"])

    def test_overlap_details_are_deterministic_for_unordered_names(self):
        # 阶段名为 z/a，但 details 中的阶段名按字符串序稳定排列。
        subject = find_overlap_subject("flag-a", "s1", 50, 100)
        request = self._request(
            stages=[{"percentage": 50, "name": "z"},
                    {"percentage": 100, "name": "a"}],
            contexts=[{"subjectKey": subject}],
        )
        with self.assertRaises(PreviewValidationError) as cm:
            self.svc.preview_change(request)
        self.assertEqual(cm.exception.details["stages"], ["a", "z"])

    def test_unknown_flag_uses_existing_not_found_semantics(self):
        request = self._request(flagKey="nope")
        with self.assertRaises(FlagNotFoundError):
            self.svc.preview_change(request)

    def test_validation_is_deterministic(self):
        request = self._request(
            contexts=[{"subjectKey": "u1"}, {"subjectKey": "u1"}])
        for _ in range(3):
            with self.assertRaises(PreviewValidationError) as cm:
                self.svc.preview_change(copy.deepcopy(request))
            self.assertEqual(cm.exception.error_code, "subject_key_duplicate")
            self.assertEqual(cm.exception.details, {"duplicates": ["u1"]})


if __name__ == "__main__":
    unittest.main()
