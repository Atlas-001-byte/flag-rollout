"""前置 Feature Flag 依赖门控（prerequisites）的单元测试，仅依赖标准库。"""

import hashlib
import unittest

from flag_rollout import (
    FeatureFlagService,
    FlagNotFoundError,
    InvalidDefinitionError,
    MissingSubjectError,
    PrerequisiteCycleError,
    PreviewValidationError,
    RevisionNotFoundError,
    RolloutConflictError,
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


class PublishPrerequisitesTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()

    def test_omitted_empty_and_null_keep_entrypoint_shape(self):
        self.svc.publish("a", 1, make_definition())
        self.svc.publish("b", 1, make_definition(prerequisites=[]))
        self.svc.publish("c", 1, make_definition(prerequisites=None))
        for key in ("a", "b", "c"):
            stored = self.svc._flags[key].revisions[1]
            self.assertEqual(stored["prerequisites"], [])
        # 既有入口行为不变：reason 仍为既有四类之一。
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "default")

    def test_prerequisites_normalized(self):
        self.svc.publish("a", 1, make_definition(prerequisites=[
            {"flagKey": "dep"},
            {"flagKey": "dep2", "revision": 3},
            {"flagKey": "dep3", "expected": False},
            {"flagKey": "dep4", "revision": 2, "expected": True},
        ]))
        self.assertEqual(
            self.svc._flags["a"].revisions[1]["prerequisites"],
            [
                {"flagKey": "dep", "revision": None, "expected": True},
                {"flagKey": "dep2", "revision": 3, "expected": True},
                {"flagKey": "dep3", "revision": None, "expected": False},
                {"flagKey": "dep4", "revision": 2, "expected": True},
            ],
        )

    def test_invalid_prerequisites_rejected_and_no_version_created(self):
        bad_prerequisites = [
            "not-a-list",
            42,
            [None],
            ["dep"],
            [{}],
            [{"revision": 1}],
            [{"flagKey": ""}],
            [{"flagKey": 1}],
            [{"flagKey": "dep", "revision": 0}],
            [{"flagKey": "dep", "revision": -2}],
            [{"flagKey": "dep", "revision": 1.5}],
            [{"flagKey": "dep", "revision": "1"}],
            [{"flagKey": "dep", "revision": True}],
            [{"flagKey": "dep", "expected": "yes"}],
            [{"flagKey": "dep", "expected": 1}],
            [{"flagKey": "dep", "expected": None}],
        ]
        for i, prereqs in enumerate(bad_prerequisites):
            with self.assertRaises(InvalidDefinitionError, msg="case %d" % i):
                self.svc.publish("bad", 1, make_definition(prerequisites=prereqs))
            # 非法 publish 不创建版本：之后同 revision 仍可成功发布。
            self.assertNotIn("bad", self.svc._flags)

    def test_duplicate_and_diamond_shaped_dependencies_are_valid(self):
        self.svc.publish("a", 1, make_definition(prerequisites=[
            {"flagKey": "dep"}, {"flagKey": "dep"}]))
        self.svc.publish("b", 1, make_definition(prerequisites=[
            {"flagKey": "a"}, {"flagKey": "a", "revision": 1}]))

    def test_published_prerequisites_are_frozen(self):
        prereqs = [{"flagKey": "dep"}]
        self.svc.publish("a", 1, make_definition(prerequisites=prereqs))
        prereqs.append({"flagKey": "tampered"})
        prereqs[0]["flagKey"] = "tampered"
        self.assertEqual(
            self.svc._flags["a"].revisions[1]["prerequisites"],
            [{"flagKey": "dep", "revision": None, "expected": True}],
        )


class EvaluatePrerequisiteTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        # dep：100% 放量，对任何带 subject_id 的上下文都开启。
        self.svc.publish("dep", 1, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True}))

    def test_satisfied_prerequisite_falls_through_to_existing_rules(self):
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        result = self.svc.evaluate("main", {"subject_id": "u1"})
        self.assertEqual(
            result, {"enabled": True, "reason": "rollout", "revision": 1,
                     "bucket": bucket_of("main", "m", "u1")})

    def test_unsatisfied_prerequisite_result_shape(self):
        # dep 关闭：主功能不再走自身规则/放量，返回门控结果。
        self.svc.publish("dep", 2, make_definition(enabled=False))
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        self.assertEqual(
            self.svc.evaluate("main", {"subject_id": "u1"}),
            {"enabled": False, "reason": "prerequisite", "revision": 1,
             "bucket": None})

    def test_revision_field_is_main_flag_revision_for_historical_evaluate(self):
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        self.svc.publish("main", 2, make_definition(
            prerequisites=[{"flagKey": "dep"}],
            rollout={"percentage": 0, "salt": "m", "serve": True}))
        # dep 当前关闭；按历史 revision=1 求值主功能，revision 仍为 1。
        self.svc.publish("dep", 3, make_definition(enabled=False))
        result = self.svc.evaluate("main", {"subject_id": "u1"}, revision=1)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["reason"], "prerequisite")
        self.assertIsNone(result["bucket"])

    def test_prerequisites_evaluated_in_list_order_and_stop_on_first_failure(self):
        # 第一项不满足即停止：第二项即便指向未知 flag 也不会被解析。
        self.svc.publish("dep-off", 1, make_definition(enabled=False))
        self.svc.publish("main", 1, make_definition(prerequisites=[
            {"flagKey": "dep-off"},
            {"flagKey": "does-not-exist"},
        ]))
        result = self.svc.evaluate("main", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "prerequisite")
        self.assertFalse(result["enabled"])

    def test_expected_false_requires_dependency_disabled(self):
        self.svc.publish("dep-off", 1, make_definition(enabled=False))
        self.svc.publish("on-when-off", 1, make_definition(
            prerequisites=[{"flagKey": "dep-off", "expected": False}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        self.assertTrue(self.svc.evaluate(
            "on-when-off", {"subject_id": "u1"})["enabled"])
        # dep 开启后 expected=False 不再满足。
        self.svc.publish("dep-off", 2, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True}))
        result = self.svc.evaluate("on-when-off", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "prerequisite")

    def test_dependency_decision_is_bool_normalized(self):
        # 依赖规则返回真值字符串 "on"，归一化为 True 后满足 expected=True。
        self.svc.publish("truthy", 1, make_definition(
            rules=[{"attribute": "x", "operator": "equals", "value": 1,
                    "serve": "on"}],
            rollout={"percentage": 0, "salt": "t", "serve": True}))
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "truthy"}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        self.assertTrue(self.svc.evaluate(
            "main", {"subject_id": "u1", "x": 1})["enabled"])
        # 未命中规则走 default=False（0 值归一化为 False）。
        self.assertEqual(
            self.svc.evaluate("main", {"subject_id": "u2"})["reason"],
            "prerequisite")

    def test_pinned_revision_is_fixed_while_current_moves(self):
        self.svc.publish("dep", 2, make_definition(enabled=False))
        # main 固定读 dep@1（开启），不受 dep 当前版本影响。
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "dep", "revision": 1}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        result = self.svc.evaluate("main", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "rollout")
        # 切到 dep 当前版本（关闭）后立即被门控挡住。
        self.svc.publish("main", 2, make_definition(
            prerequisites=[{"flagKey": "dep"}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        self.assertEqual(
            self.svc.evaluate("main", {"subject_id": "u1"})["reason"],
            "prerequisite")
        # 历史 revision=1 的求值仍固定读 dep@1。
        self.assertEqual(
            self.svc.evaluate("main", {"subject_id": "u1"}, revision=1)["reason"],
            "rollout")

    def test_recursive_prerequisites_share_same_context(self):
        # grand 命中 plan=pro 规则开启；free 用户规则落空走 0% 放量 → default。
        # child 依赖 grand；main 依赖 child，整条链共用同一 context。
        self.svc.publish("grand", 1, make_definition(
            rules=[{"attribute": "plan", "operator": "equals",
                    "value": "pro", "serve": True}]))
        self.svc.publish("child", 1, make_definition(
            prerequisites=[{"flagKey": "grand"}],
            rollout={"percentage": 100, "salt": "c", "serve": True}))
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "child"}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        self.assertTrue(self.svc.evaluate(
            "main", {"subject_id": "u1", "plan": "pro"})["enabled"])
        # 同一 context 透传：free 用户在 grand 处即被挡下。
        self.assertEqual(
            self.svc.evaluate("main", {"subject_id": "u2", "plan": "free"})["reason"],
            "prerequisite")

    def test_pinned_dependency_uses_its_own_historical_prerequisites(self):
        # mid@1 依赖 grand；mid@2 无依赖但关闭。main 固定 mid@1，grand 状态决定门控。
        self.svc.publish("grand", 1, make_definition(
            rollout={"percentage": 100, "salt": "g", "serve": True}))
        self.svc.publish("mid", 1, make_definition(
            prerequisites=[{"flagKey": "grand"}],
            rollout={"percentage": 100, "salt": "d", "serve": True}))
        self.svc.publish("mid", 2, make_definition(enabled=False))
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "mid", "revision": 1}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        self.assertTrue(self.svc.evaluate(
            "main", {"subject_id": "u1"})["enabled"])
        # grand 关闭后，固定读 mid@1 仍会递归读 grand 当前版本，门控失败。
        self.svc.publish("grand", 2, make_definition(enabled=False))
        self.assertEqual(
            self.svc.evaluate("main", {"subject_id": "u1"})["reason"],
            "prerequisite")

    def test_self_reference_to_another_revision_is_not_a_cycle(self):
        # a@1 依赖 a@2，a@2 无依赖：节点不同，构成 DAG 而非环。
        self.svc.publish("a", 1, make_definition(
            prerequisites=[{"flagKey": "a", "revision": 2}],
            rollout={"percentage": 100, "salt": "a", "serve": True}))
        self.svc.publish("a", 2, make_definition(
            rollout={"percentage": 100, "salt": "a", "serve": True}))
        # 当前版本为 2（无门控），按 revision=1 求值时递归到 a@2。
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"}, revision=1)["reason"],
            "rollout")

    def test_repeated_calls_are_deterministic(self):
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}],
            rollout={"percentage": 50, "salt": "m", "serve": True}))
        first = self.svc.evaluate("main", {"subject_id": "u1"})
        for _ in range(5):
            self.assertEqual(
                self.svc.evaluate("main", {"subject_id": "u1"}), first)


class PrerequisiteErrorTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        self.svc.publish("dep", 1, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True}))

    def test_self_cycle(self):
        self.svc.publish("self", 1, make_definition(
            prerequisites=[{"flagKey": "self"}]))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("self", {"subject_id": "u1"})

    def test_self_cycle_with_pinned_current_revision(self):
        self.svc.publish("self", 1, make_definition(
            prerequisites=[{"flagKey": "self", "revision": 1}]))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("self", {"subject_id": "u1"})

    def test_two_node_cycle(self):
        self.svc.publish("a", 1, make_definition(
            prerequisites=[{"flagKey": "b"}]))
        self.svc.publish("b", 1, make_definition(
            prerequisites=[{"flagKey": "a"}]))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("a", {"subject_id": "u1"})
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("b", {"subject_id": "u1"})

    def test_three_node_cycle_at_depth(self):
        self.svc.publish("a", 1, make_definition(
            prerequisites=[{"flagKey": "b", "revision": 1}]))
        self.svc.publish("b", 1, make_definition(
            prerequisites=[{"flagKey": "c", "revision": 1}]))
        self.svc.publish("c", 1, make_definition(
            prerequisites=[{"flagKey": "a", "revision": 1}]))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("a", {"subject_id": "u1"})

    def test_unknown_dependency_raises_flag_not_found(self):
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "ghost"}]))
        with self.assertRaises(FlagNotFoundError):
            self.svc.evaluate("main", {"subject_id": "u1"})

    def test_unknown_transitive_dependency_raises_flag_not_found(self):
        self.svc.publish("mid", 1, make_definition(
            prerequisites=[{"flagKey": "ghost"}]))
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "mid"}]))
        with self.assertRaises(FlagNotFoundError):
            self.svc.evaluate("main", {"subject_id": "u1"})

    def test_missing_dependency_revision_raises_revision_not_found(self):
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "dep", "revision": 99}]))
        with self.assertRaises(RevisionNotFoundError):
            self.svc.evaluate("main", {"subject_id": "u1"})

    def test_missing_subject_propagates_through_dependency_rollout(self):
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        with self.assertRaises(MissingSubjectError):
            self.svc.evaluate("main", {})
        with self.assertRaises(MissingSubjectError):
            self.svc.evaluate("main", {"subject_id": ""})

    def test_rule_matching_dependency_does_not_need_subject(self):
        self.svc.publish("rule-dep", 1, make_definition(
            rules=[{"attribute": "vip", "operator": "equals",
                    "value": True, "serve": True}]))
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "rule-dep"}],
            rules=[{"attribute": "vip", "operator": "equals",
                    "value": True, "serve": True}]))
        # 主功能与依赖都命中规则，不进入放量分支，无需 subject_id。
        self.assertTrue(self.svc.evaluate("main", {"vip": True})["enabled"])

    def test_errors_do_not_change_service_state(self):
        self.svc.publish("cy", 1, make_definition(
            prerequisites=[{"flagKey": "cy"}]))
        for _ in range(3):
            with self.assertRaises(PrerequisiteCycleError):
                self.svc.evaluate("cy", {"subject_id": "u1"})
        # 发布新版本不受异常影响；dep 门控路径仍可用。
        self.svc.publish("ok", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}],
            rollout={"percentage": 100, "salt": "o", "serve": True}))
        self.assertEqual(
            self.svc.evaluate("ok", {"subject_id": "u1"})["reason"], "rollout")


class SharedGatingImpactTest(unittest.TestCase):
    """rollback / promote_rollout / 多阶段计划入口共用门控，影响面只统计
    主功能 enabled 翻转的 subject_id，去重、排序与 conflict 语义不变。"""

    def setUp(self):
        self.svc = FeatureFlagService()
        # dep：100% 放量恒开。
        self.svc.publish("dep", 1, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True}))
        # main@1：0% 放量，门控 dep。
        self.svc.publish("main", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}],
            rollout={"percentage": 0, "salt": "m", "serve": True}))
        self.subjects = [{"subject_id": "u%d" % i} for i in range(40)]

    def test_promote_impact_requires_prerequisite(self):
        band = {s["subject_id"] for s in self.subjects}  # 0 -> 100 全覆盖
        result = self.svc.promote_rollout("main", 100, self.subjects, band)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(sorted(result["impacted"], key=str),
                         sorted(band, key=str))
        # dep 关闭后回滚 2 -> 1：两个版本都被门控为 False，影响面为空。
        self.svc.publish("dep", 2, make_definition(enabled=False))
        impacted = self.svc.rollback("main", 1, self.subjects, set())
        self.assertEqual(impacted, [])
        self.assertEqual(
            self.svc.evaluate("main", self.subjects[0])["revision"], 1)

    def test_promote_conflict_while_gated_off(self):
        # dep 关闭：任何放量比例下主功能恒为 False，声明影响面必然冲突。
        self.svc.publish("dep", 2, make_definition(enabled=False))
        with self.assertRaises(RolloutConflictError) as cm:
            self.svc.promote_rollout("main", 100, self.subjects, {"u1"})
        self.assertEqual(cm.exception.args[1], [])
        # 不建候选版本，当前 revision 不变。
        self.assertEqual(
            self.svc.evaluate("main", self.subjects[0])["revision"], 1)

    def test_advance_plan_respects_gating_and_dedup_sort(self):
        self.svc.create_rollout_plan("main", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100}])
        band_0_50 = {
            s["subject_id"] for s in self.subjects
            if bucket_of("main", "m", s["subject_id"]) < 5000
        }
        first = self.svc.advance_rollout_plan(
            "main", self.subjects, band_0_50)
        self.assertEqual(first["revision"], 2)
        self.assertEqual(first["impacted"], sorted(band_0_50, key=str))

        # dep 关闭后剩余阶段不再产生任何翻转：影响面为空，推进成功但无影响。
        self.svc.publish("dep", 2, make_definition(enabled=False))
        second = self.svc.advance_rollout_plan(
            "main", self.subjects, set())
        self.assertEqual(second["impacted"], [])
        self.assertTrue(second["completed"])

    def test_forecast_empty_when_gated_off(self):
        self.svc.create_rollout_plan("main", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100}])
        forecast_on = self.svc.forecast_rollout_plan("main", self.subjects)
        self.assertEqual(
            set(forecast_on["totalImpacted"]),
            {s["subject_id"] for s in self.subjects})

        self.svc.publish("dep", 2, make_definition(enabled=False))
        forecast_off = self.svc.forecast_rollout_plan("main", self.subjects)
        self.assertEqual(forecast_off["totalImpacted"], [])
        self.assertTrue(all(s["impacted"] == []
                            for s in forecast_off["stages"]))

    def test_cancel_plan_uses_shared_gating(self):
        # 两阶段计划，仅推进第一阶段到 50%（计划仍未完成，可取消）。
        self.svc.create_rollout_plan("main", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100}])
        band_0_50 = {
            s["subject_id"] for s in self.subjects
            if bucket_of("main", "m", s["subject_id"]) < 5000
        }
        self.svc.advance_rollout_plan("main", self.subjects, band_0_50)
        # 推进后把 dep 关闭：当前 revision 与基准 revision 都被门控为 False，
        # 取消计划的影响面为空。
        self.svc.publish("dep", 2, make_definition(enabled=False))
        result = self.svc.cancel_rollout_plan("main", self.subjects, set())
        self.assertEqual(result["impacted"], [])
        self.assertEqual(result["restoredRevision"], 1)

    def test_domain_errors_propagate_through_all_impact_entrypoints(self):
        # 未知依赖。
        self.svc.publish("ghost-main", 1, make_definition(
            prerequisites=[{"flagKey": "ghost"}]))
        with self.assertRaises(FlagNotFoundError):
            self.svc.rollback("ghost-main", 1, [{"subject_id": "u1"}], set())
        with self.assertRaises(FlagNotFoundError):
            self.svc.promote_rollout(
                "ghost-main", 100, [{"subject_id": "u1"}], set())

        # 成环。
        self.svc.publish("cy", 1, make_definition(
            prerequisites=[{"flagKey": "cy"}]))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.promote_rollout(
                "cy", 100, [{"subject_id": "u1"}], set())
        self.svc.create_rollout_plan("cy", [{"name": "a", "percentage": 100}])
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.advance_rollout_plan(
                "cy", [{"subject_id": "u1"}], set())
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.forecast_rollout_plan("cy", [{"subject_id": "u1"}])

        # 指定版本不存在。
        self.svc.publish("rev-main", 1, make_definition(
            prerequisites=[{"flagKey": "dep", "revision": 99}]))
        with self.assertRaises(RevisionNotFoundError):
            self.svc.rollback("rev-main", 1, [{"subject_id": "u1"}], set())

        # 缺非空 subject_id 透传。
        self.svc.publish("sub-main", 1, make_definition(
            prerequisites=[{"flagKey": "dep"}],
            rollout={"percentage": 0, "salt": "s", "serve": True}))
        with self.assertRaises(MissingSubjectError):
            self.svc.promote_rollout("sub-main", 100, [{}], set())

    def test_errors_leave_state_untouched(self):
        self.svc.publish("cy", 1, make_definition(
            prerequisites=[{"flagKey": "cy"}],
            rollout={"percentage": 0, "salt": "m", "serve": True}))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.promote_rollout(
                "cy", 100, self.subjects, set())
        # 直接核对内部状态：当前版本仍为 1，无候选版本，无计划登记。
        self.assertEqual(self.svc._flags["cy"].current, 1)
        self.assertEqual(set(self.svc._flags["cy"].revisions), {1})
        self.assertNotIn("cy", self.svc._plans)


class PreviewPrerequisiteTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        # dep@1：100% 恒开；dep@2：关闭。
        self.svc.publish("dep", 1, make_definition(
            rollout={"percentage": 100, "salt": "d", "serve": True}))
        self.svc.publish("main", 1, make_definition(
            rollout={"percentage": 0, "salt": "m", "serve": True}))

    def _request(self, definition, contexts=None, stages=None):
        return {
            "flagKey": "main",
            "definition": definition,
            "stages": stages or [{"name": "full", "percentage": 100}],
            "contexts": contexts or [{"subjectKey": "u1"}, {"subjectKey": "u2"}],
        }

    def test_satisfied_candidate_dependency_decision_true(self):
        candidate = make_definition(
            prerequisites=[{"flagKey": "dep", "revision": 1}],
            rollout={"percentage": 100, "salt": "m", "serve": True})
        response = self.svc.preview_change(self._request(candidate))
        self.assertTrue(all(item["after"] for item in response["results"]))
        self.assertTrue(all(item["stage"] == "full"
                            for item in response["results"]))
        self.assertEqual(response["summary"]["offToOn"], 2)
        self.assertEqual(
            response["summary"]["affectedSubjects"], ["u1", "u2"])

    def test_unsatisfied_candidate_dependency_decision_false(self):
        candidate = make_definition(
            prerequisites=[{"flagKey": "dep"}],  # 当前 dep 关闭
            rollout={"percentage": 100, "salt": "m", "serve": True})
        # 先关闭 dep 当前版本。
        self.svc.publish("dep", 2, make_definition(enabled=False))
        response = self.svc.preview_change(self._request(candidate))
        self.assertTrue(all(not item["after"]
                            for item in response["results"]))
        self.assertTrue(all(item["stage"] is None
                            for item in response["results"]))
        self.assertEqual(response["summary"]["stageHits"], {"full": 0})
        self.assertEqual(response["summary"]["affectedSubjects"], [])

    def test_before_and_after_use_their_own_gates(self):
        # 现行 main@1 固定依赖 dep@1（开）且 100% 放量 → before=True；
        # 候选固定依赖 dep@2（关）→ after=False：on_to_off。
        self.svc.publish("main", 2, make_definition(
            prerequisites=[{"flagKey": "dep", "revision": 1}],
            rollout={"percentage": 100, "salt": "m", "serve": True}))
        self.svc.publish("dep", 2, make_definition(enabled=False))
        candidate = make_definition(
            prerequisites=[{"flagKey": "dep", "revision": 2}],
            rollout={"percentage": 100, "salt": "m", "serve": True})
        response = self.svc.preview_change(self._request(candidate))
        self.assertEqual(response["currentRevision"], 2)
        item = response["results"][0]
        self.assertEqual((item["before"], item["after"],
                          item["changeReason"]),
                         (True, False, "on_to_off"))

    def test_candidate_depending_on_own_published_current_resolves(self):
        # 候选依赖本 flag 当前已发布版本：按当前状态解析，不与候选根节点误报成环。
        candidate = make_definition(
            prerequisites=[{"flagKey": "main"}],
            rollout={"percentage": 100, "salt": "m", "serve": True})
        response = self.svc.preview_change(self._request(
            candidate, contexts=[{"subjectKey": "u1"}]))
        # main 当前 0% 放量 → 依赖结果 False → 候选决策 False。
        self.assertFalse(response["results"][0]["after"])

    def test_domain_errors_are_not_preview_validation_errors(self):
        contexts = [{"subjectKey": "u1"}]
        cases = [
            (FlagNotFoundError,
             make_definition(prerequisites=[{"flagKey": "ghost"}])),
            (RevisionNotFoundError,
             make_definition(prerequisites=[{"flagKey": "dep",
                                             "revision": 404}] )),
        ]
        for error_type, candidate in cases:
            with self.assertRaises(error_type):
                self.svc.preview_change(
                    self._request(candidate, contexts=contexts))

        # 成环（候选经已发布 flag 回到候选所固定的同一已发布节点之外的真环）。
        self.svc.publish("cy", 1, make_definition(
            prerequisites=[{"flagKey": "cy"}]))
        request = {
            "flagKey": "cy",
            "definition": make_definition(
                prerequisites=[{"flagKey": "cy"}],
                rollout={"percentage": 100, "salt": "c", "serve": True}),
            "stages": [{"percentage": 100}],
            "contexts": contexts,
        }
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.preview_change(request)

    def test_malformed_candidate_prerequisites_are_422(self):
        contexts = [{"subjectKey": "u1"}]
        bad_candidates = [
            make_definition(prerequisites=[{"flagKey": ""}]),
            make_definition(prerequisites=[{"revision": 1}]),
            make_definition(prerequisites=[{"flagKey": "dep", "revision": 0}]),
            make_definition(prerequisites=[{"flagKey": "dep", "revision": True}]),
            make_definition(prerequisites=[{"flagKey": "dep", "expected": 1}]),
            make_definition(prerequisites="x"),
        ]
        for candidate in bad_candidates:
            with self.assertRaises(PreviewValidationError) as cm:
                self.svc.preview_change(
                    self._request(candidate, contexts=contexts))
            self.assertEqual(cm.exception.error_code, "candidate_not_evaluable")

    def test_preview_with_prerequisites_is_deterministic_and_read_only(self):
        import copy
        candidate = make_definition(
            prerequisites=[{"flagKey": "dep", "revision": 1}],
            rollout={"percentage": 50, "salt": "m", "serve": True})
        request = self._request(
            candidate,
            contexts=[{"subjectKey": "u%03d" % i} for i in range(40)])
        first = self.svc.preview_change(copy.deepcopy(request))
        second = self.svc.preview_change(copy.deepcopy(request))
        self.assertEqual(first, second)
        # 未产生新版本，当前配置与门控状态不变。
        self.assertEqual(
            self.svc.evaluate("main", {"subject_id": "u1"})["revision"], 1)
        with self.assertRaises(RevisionNotFoundError):
            self.svc.evaluate("main", {"subject_id": "u1"}, revision=2)


if __name__ == "__main__":
    unittest.main()
