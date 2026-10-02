# Flag Rollout

配置与灰度发布服务：Feature Flag 求值、渐进放量与影响面回滚。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。
纯进程内模块：不实现网络、持久化、重启恢复、数据库、并发控制或第三方依赖。

## 模块

公开入口为 `flag_rollout.FeatureFlagService`。

### publish(flag_key, revision, definition)

保存 flag_key 的正整数 revision 及其 definition，新版本成为当前版本。版本不可修改。

definition 字段：

- `enabled`：bool，总开关。
- `default`：兜底返回值。
- `rules`：规则列表，按序匹配，每项含 `attribute`、`operator`（限 `equals`、`in`、`greater_than`）、`value`、`serve`。
- `rollout`：放量配置，含 `percentage`（0–100）、`salt`、`serve`。

配置无效抛 `InvalidDefinitionError`；同一 flag_key 下 revision 重复抛 `RevisionConflictError`。

### evaluate(flag_key, context, revision=None)

返回 `{"enabled", "reason", "revision", "bucket"}`，`reason` 为 `disabled`、`rule`、`rollout`、`default` 之一。

求值顺序：规则按序匹配，首个命中项取 `serve`；未命中时 `enabled` 为 false 取 `default`；否则计算
`bucket = SHA-256(flag_key + ":" + salt + ":" + subject_id)` 前 8 字节大端整数对 10000 的余数，
小于 `percentage * 100` 取 `rollout.serve`，否则取 `default`。

- flag_key 不存在：`FlagNotFoundError`
- 指定 revision 不存在：`RevisionNotFoundError`
- 放量求值缺少非空 `subject_id`：`MissingSubjectError`

### rollback(flag_key, revision, subjects, expected_impacted)

对 subjects 逐个比较当前版本与目标 revision 的求值结果。实际受影响集合与
`expected_impacted` 不一致时抛 `RollbackConflictError` 且当前版本不变；
一致则激活目标 revision 并返回受影响主体列表。

## 测试

```bash
python3 -m pytest tests/
```

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
