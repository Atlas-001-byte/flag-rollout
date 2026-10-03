# Flag Rollout

配置与灰度发布服务：Feature Flag 求值、渐进放量与影响面回滚。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。
纯进程内模块：不实现网络、持久化、重启恢复、数据库、并发控制或第三方依赖。

## 模块

公开入口为 `flag_rollout.FeatureFlagService`，异常类型同包导出：

- `InvalidDefinitionError`：publish 的配置（flag_key / revision / definition）无效。
- `RevisionConflictError`：同一 flag_key 下 revision 已存在，版本不可修改。
- `FlagNotFoundError` / `RevisionNotFoundError`：求值或回滚时目标不存在。
- `MissingSubjectError`：放量求值需要非空 `subject_id`。
- `RollbackConflictError`：回滚实际影响面与预期不一致，当前版本不变。
- `InvalidRolloutChangeError`：promote_rollout 参数无效（percentage 类型/范围、subjects/expected_impacted 不可迭代或成员不可哈希）。
- `RolloutConflictError`：放量晋升实际影响面与预期不一致，不创建候选版本，当前版本不变。
- `PreviewValidationError`：预演请求不合法（对应 HTTP 422），携带唯一确定的 `error_code` 与 `details`。

## 用法

```python
from flag_rollout import FeatureFlagService

svc = FeatureFlagService()

# publish：保存并激活一个正整数 revision；重复 revision 抛 RevisionConflictError。
svc.publish("new-checkout", 1, {
    "enabled": True,
    "default": False,
    "rules": [  # 按序匹配，首个命中项取 serve
        {"attribute": "plan", "operator": "in", "value": ["pro", "team"], "serve": True},
        {"attribute": "age", "operator": "greater_than", "value": 18, "serve": True},
        {"attribute": "country", "operator": "equals", "value": "CN", "serve": False},
    ],
    "rollout": {"percentage": 25, "salt": "exp-7", "serve": True},
})

# evaluate：规则 → enabled → 放量桶；bucket = SHA-256(flag_key:salt:subject_id)
# 前 8 字节大端整数 % 10000，bucket < percentage * 100 时取 rollout.serve。
svc.evaluate("new-checkout", {"subject_id": "u-1", "plan": "pro"})
# -> {"enabled": True, "reason": "rule", "revision": 1, "bucket": None}
# reason ∈ {"disabled", "rule", "rollout", "default"}；未进入放量时 bucket 为 None。

# rollback：校验影响面后激活目标 revision；不一致抛 RollbackConflictError 且不改版本。
svc.rollback("new-checkout", 1,
             subjects=[{"subject_id": "u-1", "plan": "pro"}],
             expected_impacted=set())

# promote_rollout：从当前 revision 复制 definition，仅把 rollout.percentage 提到
# 50，确认影响面后创建 revision=2 并激活；返回新版本号与实际影响主体（按字符串
# 形式排序）。影响面不一致抛 RolloutConflictError（参数携带排序后的影响列表），
# 不留候选版本；percentage 非法或主体集合不可迭代/不可哈希抛
# InvalidRolloutChangeError。
svc.promote_rollout("new-checkout", 50,
                    subjects=[{"subject_id": "u-1", "plan": "pro"}],
                    expected_impacted=set())
```

指定 `revision` 的求值结果固定；已发布版本不可修改。

## 预演（preview_change）

`preview_change(request)` 是独立的只读公开入口，用来在真正提交配置前，看清同一批
请求上下文在现行规则与候选规则（含候选灰度阶段）下的求值差异。它不创建发布记录、
不推进阶段、不修改当前配置、不触发回滚，进程内也不落盘；映射到 HTTP 时合法请求
返回 200，非法请求返回 422。

请求体字段（`definition` 结构与 publish 完全一致）：

```python
svc.preview_change({
    "flagKey": "new-checkout",
    "definition": {  # 候选 flag 配置
        "enabled": True,
        "default": False,
        "rules": [
            {"id": "r-pro", "attribute": "plan", "operator": "in",
             "value": ["pro", "team"], "serve": True},
        ],
        "rollout": {"percentage": 0, "salt": "exp-7", "serve": True},
    },
    "stages": [  # 一个或多个灰度阶段，percentage 按非递减排列；可附 name
        {"name": "canary", "percentage": 10},
        {"name": "beta", "percentage": 50},
    ],
    "contexts": [  # 非空；每项至少携带非空 subjectKey，可带求值语义已识别的属性
        {"subjectKey": "u-1", "plan": "pro"},
        {"subjectKey": "u-2"},
    ],
})
```

- `subjectKey` 会映射为既有求值语义中的 `subject_id`，桶计算仍为
  `SHA-256(flag_key:salt:subject_id)`。每个候选阶段是独立放量环：省略 salt 时按
  候选 `rollout.salt` + 阶段序号确定性派生（`<salt>#preview-stage-<i>`），无随机数，
  同样输入重复调用逐项结果与汇总结果完全一致。
- 规则标识取规则可选的 `id` 字段，缺省为规则在列表中的 0 基序号；仅供预演输出。
- 未命中任何候选阶段的上下文，按候选配置在放量 0% 下求值（规则 → enabled →
  default），结论确定。
- 同一上下文在两个候选阶段同时命中（reason=rollout）时返回 422。

200 响应体：

```python
{
  "flagKey": "new-checkout",
  "currentRevision": 3,
  "results": [  # 与输入 contexts 同序、逐项稳定
    {"subjectKey": "u-1", "before": False, "after": True,
     "beforeRuleId": None, "afterRuleId": "r-pro",
     "changeReason": "off_to_on", "stage": None},
    {"subjectKey": "u-2", "before": False, "after": True,
     "beforeRuleId": None, "afterRuleId": None,
     "changeReason": "off_to_on", "stage": "canary"},
  ],
  "summary": {
    "total": 2,              # 总上下文数
    "offToOn": 2,            # 由关变开数
    "onToOff": 0,            # 由开变关数
    "unchanged": 0,          # 决策未变数
    "stageHits": {"canary": 1, "beta": 0},  # 候选阶段内命中数
    "affectedSubjects": ["u-1", "u-2"],     # 受影响 subjectKey，按字符串序排列
  },
}
```

`changeReason ∈ {"off_to_on", "on_to_off", "unchanged"}`；`stage` 为命中的阶段名
（默认 `stage-<i>`），未命中为 `None`。逐项中的布尔决策由 serve 值按 `bool(...)`
归一化得到，与现有"仅 enabled 翻转计入影响"的语义一致。

422 错误码（`PreviewValidationError.error_code`，唯一且确定）：

| errorCode | 触发条件 |
|---|---|
| `invalid_payload` | 请求体不是对象（HTTP 下即输入不是数组/对象等整体形状错误） |
| `flag_key_empty` | `flagKey` 为空或非字符串 |
| `contexts_not_list` | `contexts` 不是数组 |
| `contexts_empty` | `contexts` 为空数组 |
| `context_not_object` | 某个 context 不是对象 |
| `subject_key_missing` | 某上下文缺少非空 `subjectKey`（含不可哈希标量） |
| `subject_key_duplicate` | `subjectKey` 重复 |
| `stages_not_list` | `stages` 不是数组 |
| `stages_empty` | `stages` 为空数组 |
| `stage_not_object` | 某个 stage 不是对象 |
| `stage_name_invalid` | stage `name` 不是非空字符串 |
| `stage_name_duplicate` | 阶段名称重复 |
| `stage_percentage_invalid` | 阶段比例不是 [0, 100] 内数值 |
| `stage_percentages_not_non_decreasing` | 阶段比例未按非递减排列 |
| `stage_overlap` | 同一上下文在两个候选阶段同时命中 |
| `candidate_not_evaluable` | 候选配置无法按既有语义求值（结构非法或缺 subject_id） |

未知 `flagKey` 沿用现有 `FlagNotFoundError`（与 evaluate/rollback/promote_rollout
一致，不纳入 422 错误码表）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
