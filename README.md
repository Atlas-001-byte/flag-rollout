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

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
