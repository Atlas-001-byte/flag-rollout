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
    RevisionConflictError,
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
        "rollout": {"percentage": 100, "salt": "s1", "serve": True},
    }
    definition.update(overrides)
    return definition


# 常用依赖：关闭（enabled=False，恒 False，不需要 subject_id）/
# 全量开启（100% 放量，进入放量需要非空 subject_id）。
def dep_off(**prereq_overrides):
    return make_definition(enabled=False, default=False,
                           rollout={"percentage": 0, "salt": "d", "serve": True},
                           **prereq_overrides)


def dep_on(**prereq_overrides):
    return make_definition(enabled=True, default=False,
                           rollout={"percentage": 100, "salt": "d", "serve": True},
                           **prereq_overrides)


class PrerequisitesValidationTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()

    def test_missing_or_empty_prerequisites_keeps_baseline(self):
        self.svc.publish("a", 1, make_definition())
        self.svc.publish("b", 1, make_definition(prerequisites=[]))
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "rollout")
        self.assertEqual(
            self.svc.evaluate("b", {"subject_id": "u1"})["reason"], "rollout"
        )

    def test_valid_prerequisites_publish(self):
        self.svc.publish("dep", 1, dep_off())
        for prerequisites in (
            [{"flagKey": "dep"}],
            [{"flagKey": "dep", "expected": True}],
            [{"flagKey": "dep", "expected": False}],
            [{"flagKey": "dep", "revision": 1}],
            [{"flagKey": "dep", "revision": 1, "expected": False}],
            [{"flagKey": "dep"}, {"flagKey": "dep", "revision": 1, "expected": True}],
        ):
            revision = len(self.svc._flags["a"].revisions) + 1 if "a" in self.svc._flags else 1
            self.svc.publish("a", revision, make_definition(prerequisites=prerequisites))

    def test_expected_defaults_to_true_and_is_normalized(self):
        self.svc.publish("dep", 1, dep_off())
        self.svc.publish("a", 1, make_definition(prerequisites=[{"flagKey": "dep"}]))
        # dep 关闭、expected 缺省为 True：门控不满足。
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(
            result,
            {"enabled": False, "reason": "prerequisite", "revision": 1, "bucket": None},
        )
        # 规范化后的定义补齐了 expected=True。
        self.assertEqual(
            self.svc._flags["a"].revisions[1]["prerequisites"],
            [{"flagKey": "dep", "revision": None, "expected": True}],
        )

    def test_invalid_prerequisites(self):
        bad_cases = [
            "not-a-list",
            {"flagKey": "dep"},
            42,
            ["x"],
            [{}],                                   # 缺 flagKey
            [{"flagKey": ""}],                       # 空 flagKey
            [{"flagKey": 7}],                        # 非字符串 flagKey
            [{"flagKey": "dep", "revision": 0}],
            [{"flagKey": "dep", "revision": -1}],
            [{"flagKey": "dep", "revision": 1.5}],
            [{"flagKey": "dep", "revision": "2"}],
            [{"flagKey": "dep", "revision": True}],
            [{"flagKey": "dep", "revision": None}],  # 显式 null 非法
            [{"flagKey": "dep", "expected": "yes"}],
            [{"flagKey": "dep", "expected": 1}],
            [{"flagKey": "dep", "expected": None}],
        ]
        self.svc.publish("dep", 1, dep_off())
        for i, bad in enumerate(bad_cases):
            with self.assertRaises(InvalidDefinitionError, msg="case %d %r" % (i, bad)):
                self.svc.publish("a", i + 1, make_definition(prerequisites=bad))

    def test_invalid_publish_creates_no_revision(self):
        with self.assertRaises(InvalidDefinitionError):
            self.svc.publish("a", 1, make_definition(prerequisites=[{"flagKey": ""}]))
        self.assertNotIn("a", self.svc._flags)
        # 同 revision 之后仍可正常发布（失败未占用版本）。
        self.svc.publish("a", 1, make_definition())
        self.assertEqual(self.svc._flags["a"].current, 1)

    def test_self_reference_is_allowed_at_publish_but_fails_at_evaluate(self):
        # 环是运行期求值概念：定义形状合法即可发布。
        self.svc.publish("a", 1, make_definition(prerequisites=[{"flagKey": "a"}]))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("a", {"subject_id": "u1"})

    def test_published_prerequisites_are_frozen(self):
        prerequisites = [{"flagKey": "dep"}]
        self.svc.publish("dep", 1, dep_on())
        self.svc.publish("a", 1, make_definition(prerequisites=prerequisites))
        prerequisites.append({"flagKey": "other"})
        prerequisites[0]["expected"] = False
        self.assertEqual(
            self.svc._flags["a"].revisions[1]["prerequisites"],
            [{"flagKey": "dep", "revision": None, "expected": True}],
        )


class PrerequisiteGateEvaluateTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()

    def test_satisfied_prerequisite_follows_existing_order(self):
        self.svc.publish("dep", 1, dep_on())
        self.svc.publish("a", 1, make_definition(prerequisites=[{"flagKey": "dep"}]))
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(result["enabled"], True)
        self.assertEqual(result["reason"], "rollout")
        self.assertEqual(result["revision"], 1)
        # 依赖满足后桶与无依赖时完全一致。
        self.assertEqual(result["bucket"], bucket_of("a", "s1", "u1"))

    def test_unsatisfied_prerequisite_shape(self):
        self.svc.publish("dep", 1, dep_off())
        self.svc.publish("a", 7, make_definition(prerequisites=[{"flagKey": "dep"}]))
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(
            result,
            {"enabled": False, "reason": "prerequisite", "revision": 7, "bucket": None},
        )

    def test_expected_false_inverts_requirement(self):
        self.svc.publish("dep", 1, dep_off())
        self.svc.publish(
            "a", 1,
            make_definition(prerequisites=[{"flagKey": "dep", "expected": False}]),
        )
        # dep=False 且 expected=False：满足，主功能正常开启。
        self.assertTrue(self.svc.evaluate("a", {"subject_id": "u1"})["enabled"])

        self.svc.publish("dep", 2, dep_on())
        # dep 切到当前 rev2=True：expected=False 不再满足。
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "prerequisite")
        self.assertFalse(result["enabled"])

    def test_gate_runs_before_rule_match(self):
        # 主功能规则本会命中，但门控在规则之前：依赖不满足仍短路。
        self.svc.publish("dep", 1, dep_off())
        self.svc.publish("a", 1, make_definition(
            rules=[{"attribute": "vip", "operator": "equals",
                    "value": True, "serve": True}],
            prerequisites=[{"flagKey": "dep"}]))
        result = self.svc.evaluate("a", {"subject_id": "u1", "vip": True})
        self.assertEqual(
            result,
            {"enabled": False, "reason": "prerequisite", "revision": 1, "bucket": None},
        )

    def test_short_circuit_stops_at_first_unsatisfied(self):
        self.svc.publish("dep", 1, dep_off())
        # 第二项指向未发布 flag；第一项不满足时不得求值第二项（否则抛 not found）。
        self.svc.publish("a", 1, make_definition(prerequisites=[
            {"flagKey": "dep"}, {"flagKey": "ghost"},
        ]))
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "prerequisite")
        # 第一项满足后，第二项的未发布依赖才被解析并报错。
        self.svc.publish("dep", 2, dep_on())
        with self.assertRaises(FlagNotFoundError):
            self.svc.evaluate("a", {"subject_id": "u1"})

    def test_all_satisfied_then_rules_enabled_rollout_default(self):
        self.svc.publish("d1", 1, dep_on())
        self.svc.publish("d2", 1, dep_on())
        # 多依赖全满足；主功能 enabled=False 走 default（门控通过后沿用既有顺序）。
        self.svc.publish("a", 1, make_definition(
            enabled=False, default="off",
            prerequisites=[{"flagKey": "d1"}, {"flagKey": "d2", "expected": True}]))
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual((result["enabled"], result["reason"]), ("off", "disabled"))

    def test_prerequisite_compares_raw_enabled_value_not_truthiness(self):
        # 与影响面“enabled 原始值直接比较”的既有口径一致：依赖规则 serve 为真值
        # 非布尔字符串 "on" 时，"on" != True（严格相等），expected=True 判定不满足。
        self.svc.publish("dep", 1, make_definition(
            rules=[{"attribute": "x", "operator": "equals", "value": 1,
                    "serve": "on"}],
            rollout={"percentage": 0, "salt": "d", "serve": True}))
        self.svc.publish(
            "a", 1,
            make_definition(prerequisites=[{"flagKey": "dep"}]),
        )
        result = self.svc.evaluate("a", {"subject_id": "u1", "x": 1})
        self.assertEqual(result["reason"], "prerequisite")
        self.assertFalse(result["enabled"])
        # expected 也无法写成字符串（校验只接受布尔），故门控始终按布尔严格比较。

    def test_main_rollout_missing_subject_after_satisfied_gate(self):
        # 依赖以规则命中（不需要 subject）而满足；主功能进入放量仍缺 subject_id。
        self.svc.publish("dep", 1, make_definition(
            rules=[{"attribute": "ok", "operator": "equals", "value": True,
                    "serve": True}]))
        self.svc.publish(
            "a", 1, make_definition(prerequisites=[{"flagKey": "dep"}])
        )
        with self.assertRaises(MissingSubjectError):
            self.svc.evaluate("a", {"ok": True})


class PrerequisiteRevisionTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        # dep rev1 关闭、rev2 全量开启，当前为 rev2。
        self.svc.publish("dep", 1, dep_off())
        self.svc.publish("dep", 2, dep_on())

    def test_pinned_revision_reads_that_version(self):
        self.svc.publish(
            "a", 1,
            make_definition(prerequisites=[{"flagKey": "dep", "revision": 1}]),
        )
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "prerequisite")  # rev1=False 不满足

    def test_omitted_revision_reads_current(self):
        self.svc.publish(
            "a", 1, make_definition(prerequisites=[{"flagKey": "dep"}])
        )
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "rollout")  # current rev2=True 满足

    def test_pinned_revision_tracks_current_change(self):
        # 指定 rev2 时满足；发布 rev3（关闭）并激活后，固定 rev2 仍满足。
        self.svc.publish("dep", 3, dep_off())
        self.svc.publish(
            "a", 1,
            make_definition(prerequisites=[{"flagKey": "dep", "revision": 2}]),
        )
        pinned = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(pinned["reason"], "rollout")
        current = self.svc.evaluate(
            "a", {"subject_id": "u1"},
        )
        # 不固定时读当前 rev3=False → 不满足。
        self.svc.publish(
            "b", 1, make_definition(prerequisites=[{"flagKey": "dep"}])
        )
        self.assertEqual(
            self.svc.evaluate("b", {"subject_id": "u1"})["reason"], "prerequisite"
        )

    def test_missing_revision_raises_not_found(self):
        self.svc.publish(
            "a", 1,
            make_definition(prerequisites=[{"flagKey": "dep", "revision": 99}]),
        )
        with self.assertRaises(RevisionNotFoundError):
            self.svc.evaluate("a", {"subject_id": "u1"})

    def test_historical_main_revision_uses_its_own_prerequisites(self):
        # 主功能 rev1 固定依赖 dep rev1（关闭）；rev2 固定依赖 dep rev2（开启）。
        self.svc.publish(
            "a", 1,
            make_definition(prerequisites=[{"flagKey": "dep", "revision": 1}]),
        )
        self.svc.publish(
            "a", 2,
            make_definition(prerequisites=[{"flagKey": "dep", "revision": 2}]),
        )
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"}, revision=1)["reason"],
            "prerequisite",
        )
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"}, revision=2)["reason"],
            "rollout",
        )
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"})["revision"], 2
        )


class TransitivePrerequisiteTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()

    def test_nested_dependencies_recurse(self):
        # c 关闭 → b 门控失败(enabled=False) → a 门控失败。
        self.svc.publish("c", 1, dep_off())
        self.svc.publish("b", 1, dep_on(prerequisites=[{"flagKey": "c"}]))
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "b"}]))
        result = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(result["reason"], "prerequisite")
        self.assertFalse(result["enabled"])
        # b 自身同样短路，revision 取 b 的版本号。
        b_result = self.svc.evaluate("b", {"subject_id": "u1"})
        self.assertEqual(
            b_result,
            {"enabled": False, "reason": "prerequisite", "revision": 1, "bucket": None},
        )

    def test_nested_chain_all_satisfied(self):
        self.svc.publish("c", 1, dep_on())
        self.svc.publish("b", 1, dep_on(prerequisites=[{"flagKey": "c"}]))
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "b"}]))
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"})["reason"], "rollout"
        )

    def test_nested_expected_false(self):
        # b 期望 c 关闭；c 关闭时链路成立。
        self.svc.publish("c", 1, dep_off())
        self.svc.publish(
            "b", 1,
            dep_on(prerequisites=[{"flagKey": "c", "expected": False}]),
        )
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "b"}]))
        self.assertTrue(self.svc.evaluate("a", {"subject_id": "u1"})["enabled"])

    def test_nested_pinned_revision(self):
        self.svc.publish("c", 1, dep_off())
        self.svc.publish("c", 2, dep_on())
        # b 固定依赖 c rev1（关闭）→ b 不满足 → a 不满足。
        self.svc.publish(
            "b", 1,
            dep_on(prerequisites=[{"flagKey": "c", "revision": 1}]),
        )
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "b"}]))
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"})["reason"], "prerequisite"
        )
        # b 固定改为 rev2（开启）后链路满足。
        self.svc.publish(
            "b", 2,
            dep_on(prerequisites=[{"flagKey": "c", "revision": 2}]),
        )
        # a 不固定 revision，读到 b 当前 rev2。
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"})["reason"], "rollout"
        )


class PrerequisiteCycleTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()

    def test_self_cycle(self):
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "a"}]))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("a", {"subject_id": "u1"})

    def test_two_node_cycle(self):
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "b"}]))
        self.svc.publish("b", 1, dep_on(prerequisites=[{"flagKey": "a"}]))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("a", {"subject_id": "u1"})
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("b", {"subject_id": "u1"})

    def test_three_node_cycle(self):
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "b"}]))
        self.svc.publish("b", 1, dep_on(prerequisites=[{"flagKey": "c"}]))
        self.svc.publish("c", 1, dep_on(prerequisites=[{"flagKey": "a"}]))
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("a", {"subject_id": "u1"})

    def test_cycle_after_satisfied_earlier_dependency_is_still_detected(self):
        self.svc.publish("ok", 1, dep_on())
        self.svc.publish("b", 1, dep_on(prerequisites=[{"flagKey": "a"}]))
        self.svc.publish("a", 1, dep_on(prerequisites=[
            {"flagKey": "ok"}, {"flagKey": "b"},
        ]))
        # 第一项满足，继续解析第二项时才成环。
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("a", {"subject_id": "u1"})

    def test_cycle_error_does_not_change_state(self):
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "b"}]))
        self.svc.publish("b", 1, dep_on(prerequisites=[{"flagKey": "a"}]))
        self.svc.publish("solo", 1, dep_on())
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.evaluate("a", {"subject_id": "u1"})
        # 无关 flag 求值与已发布版本均不受影响。
        self.assertTrue(self.svc.evaluate("solo", {"subject_id": "u1"})["enabled"])
        # 发布无环新版本即可恢复（异常未污染状态）。
        self.svc.publish("a", 2, dep_on(prerequisites=[{"flagKey": "solo"}]))
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"})["reason"], "rollout"
        )


class PrerequisiteErrorTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()

    def test_unknown_prerequisite_flag(self):
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "ghost"}]))
        with self.assertRaises(FlagNotFoundError):
            self.svc.evaluate("a", {"subject_id": "u1"})

    def test_unknown_nested_prerequisite_flag(self):
        self.svc.publish("b", 1, dep_on(prerequisites=[{"flagKey": "ghost"}]))
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "b"}]))
        with self.assertRaises(FlagNotFoundError):
            self.svc.evaluate("a", {"subject_id": "u1"})

    def test_nested_missing_revision(self):
        self.svc.publish("c", 1, dep_on())
        self.svc.publish(
            "b", 1,
            dep_on(prerequisites=[{"flagKey": "c", "revision": 5}]),
        )
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "b"}]))
        with self.assertRaises(RevisionNotFoundError):
            self.svc.evaluate("a", {"subject_id": "u1"})

    def test_dependency_entering_rollout_requires_subject(self):
        # 主功能 enabled=False（自身不需要 subject），但依赖全量开启需要 subject。
        self.svc.publish("dep", 1, dep_on())
        self.svc.publish(
            "a", 1,
            make_definition(enabled=False, default=False,
                            prerequisites=[{"flagKey": "dep"}]),
        )
        with self.assertRaises(MissingSubjectError):
            self.svc.evaluate("a", {})
        with self.assertRaises(MissingSubjectError):
            self.svc.evaluate("a", {"subject_id": ""})
        # 依赖以规则命中（不需要 subject）时，主功能 enabled=False 无 subject 也可求值。
        self.svc.publish(
            "dep2", 1,
            make_definition(rules=[{"attribute": "vip", "operator": "equals",
                                    "value": True, "serve": True}]),
        )
        self.svc.publish(
            "a2", 1,
            make_definition(enabled=False, default=False,
                            prerequisites=[{"flagKey": "dep2"}]),
        )
        result = self.svc.evaluate("a2", {"vip": True})
        self.assertEqual(result["reason"], "disabled")

    def test_errors_keep_state(self):
        self.svc.publish("a", 1, dep_on(prerequisites=[{"flagKey": "ghost"}]))
        for _ in range(3):
            with self.assertRaises(FlagNotFoundError):
                self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(self.svc._flags["a"].current, 1)
        # 补齐依赖后可正常求值。
        self.svc.publish("ghost", 1, dep_on())
        self.assertTrue(self.svc.evaluate("a", {"subject_id": "u1"})["enabled"])


class SharedEntrypointGateTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        self.subjects = [{"subject_id": "u%d" % i} for i in range(20)]

    def test_promote_rollout_applies_gate(self):
        self.svc.publish("dep", 1, dep_off())
        self.svc.publish(
            "a", 1,
            make_definition(rollout={"percentage": 0, "salt": "s1", "serve": True},
                            prerequisites=[{"flagKey": "dep"}]),
        )
        # dep 关闭：0% → 100% 候选副本仍带门控，无人翻转，影响面为空。
        result = self.svc.promote_rollout("a", 100, self.subjects, set())
        self.assertEqual(result, {"revision": 2, "impacted": []})
        gated = self.svc.evaluate("a", {"subject_id": "u1"})
        self.assertEqual(gated["reason"], "prerequisite")
        # dep 开启后，已是 100% 的当前版本门控通过即放量开启。
        self.svc.publish("dep", 2, dep_on())
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"})["reason"], "rollout"
        )

    def test_rollback_applies_gate(self):
        # rev1 无门控全量开启；rev2 带门控（dep 关闭）恒 False。
        self.svc.publish("a", 1, make_definition())
        self.svc.publish("dep", 1, dep_off())
        self.svc.publish(
            "a", 2, make_definition(prerequisites=[{"flagKey": "dep"}])
        )
        impacted = self.svc.rollback(
            "a", 1, self.subjects,
            {"u%d" % i for i in range(20)},
        )
        self.assertEqual(impacted, sorted({"u%d" % i for i in range(20)}, key=str))
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"})["revision"], 1
        )

    def test_plan_advance_forecast_cancel_apply_gate(self):
        self.svc.publish("dep", 1, dep_off())
        self.svc.publish(
            "a", 1,
            make_definition(rollout={"percentage": 0, "salt": "s1", "serve": True},
                            prerequisites=[{"flagKey": "dep"}]),
        )
        self.svc.create_rollout_plan("a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        # forecast：门控关闭，所有阶段影响面为空，且只读。
        forecast = self.svc.forecast_rollout_plan("a", self.subjects)
        self.assertTrue(all(s["impacted"] == [] for s in forecast["stages"]))
        self.assertEqual(forecast["totalImpacted"], [])
        # advance：影响面为空也能推进（候选副本带门控）。
        advanced = self.svc.advance_rollout_plan("a", self.subjects, set())
        self.assertEqual(advanced["impacted"], [])
        self.assertEqual(advanced["revision"], 2)
        # cancel：当前(rev2,门控关) 与基准(rev1,门控关) 都 False，影响面为空。
        cancelled = self.svc.cancel_rollout_plan("a", self.subjects, set())
        self.assertEqual(cancelled["impacted"], [])
        self.assertEqual(cancelled["restoredRevision"], 1)

    def test_gate_opens_after_dependency_enabled_impacts_main_only(self):
        # dep 开启后推进放量：只有主功能 enabled 翻转计入影响，桶口径不变。
        self.svc.publish("dep", 1, dep_on())
        self.svc.publish(
            "a", 1,
            make_definition(rollout={"percentage": 0, "salt": "s1", "serve": True},
                            prerequisites=[{"flagKey": "dep"}]),
        )
        expected = {s["subject_id"] for s in self.subjects
                    if bucket_of("a", "s1", s["subject_id"]) < 5000}
        result = self.svc.promote_rollout("a", 50, self.subjects, expected)
        self.assertEqual(set(result["impacted"]), expected)

    def test_shared_entrypoints_propagate_prerequisite_errors(self):
        self.svc.publish("dep", 1, dep_on())
        self.svc.publish(
            "a", 1,
            make_definition(rollout={"percentage": 0, "salt": "s1", "serve": True},
                            prerequisites=[{"flagKey": "ghost"}]),
        )
        # promote：未知依赖抛 FlagNotFoundError，不建版本。
        with self.assertRaises(FlagNotFoundError):
            self.svc.promote_rollout("a", 100, self.subjects, set())
        self.assertEqual(self.svc._flags["a"].current, 1)
        with self.assertRaises(KeyError):
            self.svc._flags["a"].revisions[2]

        # rollback：同样抛错且当前版本不变。
        with self.assertRaises(FlagNotFoundError):
            self.svc.rollback("a", 1, self.subjects, set())
        self.assertEqual(self.svc._flags["a"].current, 1)

        # 计划入口：建计划本身不求值依赖；forecast/advance 求值时抛错且不推进。
        self.svc.create_rollout_plan("a", [{"name": "full", "percentage": 100}])
        with self.assertRaises(FlagNotFoundError):
            self.svc.forecast_rollout_plan("a", self.subjects)
        with self.assertRaises(FlagNotFoundError):
            self.svc.advance_rollout_plan("a", self.subjects, set())
        plan = self.svc._plans["a"]
        self.assertEqual(plan.cursor, 0)
        self.assertEqual(plan.confirmed_revision, 1)

    def test_cancel_missing_subject_in_dependency_raises_and_keeps_state(self):
        self.svc.publish("dep", 1, dep_on())
        self.svc.publish(
            "a", 1,
            make_definition(rollout={"percentage": 0, "salt": "s1", "serve": True},
                            prerequisites=[{"flagKey": "dep"}]),
        )
        # 两阶段计划，仅推进一阶段，计划仍未完成，可取消。
        self.svc.create_rollout_plan("a", [
            {"name": "canary", "percentage": 50},
            {"name": "full", "percentage": 100},
        ])
        canary = {s["subject_id"] for s in self.subjects
                  if bucket_of("a", "s1", s["subject_id"]) < 5000}
        self.svc.advance_rollout_plan("a", self.subjects, canary)
        # 依赖进入放量却缺非空 subject_id：抛 MissingSubjectError，状态不变。
        with self.assertRaises(MissingSubjectError):
            self.svc.cancel_rollout_plan("a", [{}], set())
        self.assertEqual(self.svc._flags["a"].current, 2)
        self.assertIn("a", self.svc._plans)


class PreviewPrerequisiteTest(unittest.TestCase):
    def setUp(self):
        self.svc = FeatureFlagService()
        # 现行 a：enabled、0%、无依赖 → before=False。
        self.svc.publish(
            "a", 1,
            make_definition(rollout={"percentage": 0, "salt": "s1", "serve": True}),
        )

    def _request(self, definition, contexts=None):
        return {
            "flagKey": "a",
            "definition": definition,
            "stages": [{"name": "full", "percentage": 100}],
            "contexts": contexts or [{"subjectKey": "u1"}, {"subjectKey": "u2"}],
        }

    def test_candidate_satisfied_prerequisite_evaluates_normally(self):
        self.svc.publish("dep", 1, dep_on())
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True},
            prerequisites=[{"flagKey": "dep"}],
        )
        response = self.svc.preview_change(self._request(candidate))
        self.assertEqual(response["summary"]["offToOn"], 2)
        self.assertTrue(all(r["after"] for r in response["results"]))
        self.assertTrue(all(r["stage"] == "full" for r in response["results"]))

    def test_candidate_unsatisfied_prerequisite_decides_false(self):
        self.svc.publish("dep", 1, dep_off())
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True},
            prerequisites=[{"flagKey": "dep"}],
        )
        response = self.svc.preview_change(self._request(candidate))
        # 门控不满足：候选决策恒 False，即使 100% 阶段也不命中。
        self.assertTrue(all(r["after"] is False for r in response["results"]))
        self.assertTrue(all(r["changeReason"] == "unchanged"
                            for r in response["results"]))
        self.assertEqual(response["summary"]["stageHits"], {"full": 0})

    def test_candidate_expected_false(self):
        self.svc.publish("dep", 1, dep_off())
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True},
            prerequisites=[{"flagKey": "dep", "expected": False}],
        )
        response = self.svc.preview_change(self._request(candidate))
        self.assertEqual(response["summary"]["offToOn"], 2)

    def test_before_uses_current_prerequisites(self):
        # 现行版本 rev2 带门控且 dep 关闭 → before=False；候选无门控 100% → after True。
        self.svc.publish("dep", 1, dep_off())
        self.svc.publish(
            "a", 2,
            make_definition(rollout={"percentage": 0, "salt": "s1", "serve": True},
                            prerequisites=[{"flagKey": "dep"}]),
        )
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True})
        response = self.svc.preview_change(self._request(candidate))
        self.assertTrue(all(r["before"] is False and r["after"] is True
                            for r in response["results"]))

    def test_prerequisite_resolves_against_current_state(self):
        # 预演时 dep 关闭 → after False；发布 dep 开启后重复预演 → after True。
        self.svc.publish("dep", 1, dep_off())
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True},
            prerequisites=[{"flagKey": "dep"}],
        )
        first = self.svc.preview_change(self._request(candidate))
        self.assertTrue(all(r["after"] is False for r in first["results"]))
        self.svc.publish("dep", 2, dep_on())
        second = self.svc.preview_change(self._request(candidate))
        self.assertTrue(all(r["after"] is True for r in second["results"]))

    def test_pinned_prerequisite_revision_in_preview(self):
        self.svc.publish("dep", 1, dep_off())
        self.svc.publish("dep", 2, dep_on())
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True},
            prerequisites=[{"flagKey": "dep", "revision": 1}],
        )
        response = self.svc.preview_change(self._request(candidate))
        self.assertTrue(all(r["after"] is False for r in response["results"]))

    def test_prerequisite_errors_are_not_422(self):
        # 未知依赖。
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True},
            prerequisites=[{"flagKey": "ghost"}],
        )
        with self.assertRaises(FlagNotFoundError):
            self.svc.preview_change(self._request(candidate))

        # 依赖版本不存在。
        self.svc.publish("dep", 1, dep_on())
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True},
            prerequisites=[{"flagKey": "dep", "revision": 99}],
        )
        with self.assertRaises(RevisionNotFoundError):
            self.svc.preview_change(self._request(candidate))

        # 已发布依赖图成环：x -> y -> x。
        self.svc.publish("x", 1, dep_on(prerequisites=[{"flagKey": "y"}]))
        self.svc.publish("y", 1, dep_on(prerequisites=[{"flagKey": "x"}]))
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True},
            prerequisites=[{"flagKey": "x"}],
        )
        with self.assertRaises(PrerequisiteCycleError):
            self.svc.preview_change(self._request(candidate))

    def test_malformed_candidate_prerequisites_is_422(self):
        for bad in (
            "not-a-list",
            [{"flagKey": ""}],
            [{"flagKey": 7}],
            [{"flagKey": "dep", "revision": 1.5}],
            [{"flagKey": "dep", "revision": None}],
            [{"flagKey": "dep", "expected": "yes"}],
            [{}],
        ):
            candidate = make_definition(
                rollout={"percentage": 0, "salt": "s1", "serve": True},
                prerequisites=bad,
            )
            with self.assertRaises(PreviewValidationError) as cm:
                self.svc.preview_change(self._request(candidate))
            self.assertEqual(cm.exception.error_code, "candidate_not_evaluable")

    def test_preview_with_prerequisites_is_deterministic_and_read_only(self):
        self.svc.publish("dep", 1, dep_on())
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True},
            prerequisites=[{"flagKey": "dep"}],
        )
        request = self._request(
            candidate, contexts=[{"subjectKey": "u%03d" % i} for i in range(30)]
        )
        import copy
        first = self.svc.preview_change(copy.deepcopy(request))
        second = self.svc.preview_change(copy.deepcopy(request))
        self.assertEqual(first, second)
        # 只读：未新建版本、未改当前版本。
        self.assertEqual(self.svc._flags["a"].current, 1)
        self.assertEqual(set(self.svc._flags["a"].revisions), {1})

    def test_failed_gate_preview_keeps_state(self):
        candidate = make_definition(
            rollout={"percentage": 0, "salt": "s1", "serve": True},
            prerequisites=[{"flagKey": "ghost"}],
        )
        with self.assertRaises(FlagNotFoundError):
            self.svc.preview_change(self._request(candidate))
        # 异常后现行入口与正常预演仍可用，状态未变。
        self.assertEqual(
            self.svc.evaluate("a", {"subject_id": "u1"})["revision"], 1
        )
        ok = make_definition(rollout={"percentage": 0, "salt": "s1", "serve": True})
        response = self.svc.preview_change(self._request(ok))
        self.assertEqual(response["currentRevision"], 1)


if __name__ == "__main__":
    unittest.main()
