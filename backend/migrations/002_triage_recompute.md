# 002 实判修订与当班分流清单重算

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，
升级到修改过字段的版本需要显式迁移。本迁移只新增表，不改动既有表结构。

## 背景

同一交接单的实判更正带修订号，新旧修订可能乱序到货。需要支持当班分流清单的重算：

- 训练、验证、留出仍按闸口初检日归段，每段只取该段截止前实际入账的最高修订；
- 已派单交接单保留派单当时的实判依据；
- 重算期间沿用旧清单，整份算完后一次性原子切换；
- 并发重算时受理序号小的任务即使晚完成也不能覆盖较新清单；
- 失败重试不重复派单。

## 新增表

- `audit_revisions` 实判更正流水。`(ticket_id, revision)` 唯一，修订号严格递增；
  `posted_at` 记录实际入账日，截止点可见性按入账日判定，乱序到货安全。
- `triage_lists` 分流清单版本。`seq` 为单调受理序号（唯一），状态机
  `queued → running → active / failed / superseded`；同一时刻至多一个 `active`，
  切换在单事务内把旧 active 置为 superseded 并激活新清单。
- `dispatch_records` 派单回执。`(list_id, ticket_id)` 唯一保证派单幂等，
  `evidence_snapshot` 冻结派单当时采用的修订号与通道，重算不得改写。

## 兼容性

- 既有 `runs` / `deliveries` 流程不变；新流程通过 `/triage/*` 路由提供。
- 导入批次 CSV 中已填写的 `audit_date/audit_outcome` 视为修订号 0 的初版实判，
  无修订流水时重算结果与原分析引擎完全一致（由 `tests/test_triage_engine.py` 等价性校验覆盖）。
