# 1.7 完整历史基线与状态识别

> 修订提示（2026-10-04）：本文所记账本历史识别口径已由 design D11 修订，见 verification/09-deployment-revision.md。

日期：2026-10-02。参考提交 `2d7f4a585222a3dc2f9d57e86baf7c9f925c3c78` 已通过只读
`git rev-parse <提交>^{commit}` 确认。**基线内容取当前工作树原内容**，不是把该提交
误当成已经保存了所有未跟踪实现；逐项提交关系见 MANIFEST。

## 原内容保存与一致性

位置：`history/snowluma-forward-service/`，清单：[MANIFEST.md](../../../../../history/snowluma-forward-service/MANIFEST.md)。

- 接续前80项已存在，全部与原文件逐字节一致，未覆盖。
- 仅补29项，最终109项（108个普通文件、1个 `CLAUDE.md → AGENTS.md` 软链接），
  总计1,633,141字节，软链接按目标文本长度计算。MANIFEST 自身不计入109项。
- 覆盖全部根 `.py`、`service/`、`shared/`、`tests/` 源码，服务环境 `pyproject.toml`、
  清单/公开配置、类型检查与pytest入口、原规则/README/术语和必要服务文档。
  `clock.py/config.py/plugin.py/delivery_ledger.py`、SDK替身及测试辅助依赖均已补齐。
- 每项记录原路径、用途、SHA-256、原始字节数、与基准提交关系、保留/复用/隔离去向；
  以同相对路径保存完整内容。源码静态 AST 本地 import 闭包无缺项；不导入或运行历史。
- 原活跃代码、测试、配置、规则均未改写；原路径也没有移动/删除。历史收集隔离归第7组，
  本组不能假称已经退出默认收集或仍维护独立服务产品。
- 未复制 `.local/.git/__pycache__`、虚拟环境、数据库、日志、凭证或远端运行数据。

实际核对：Python内存程序读取允许源码清单，对既有目标逐字节比较；仅用排他创建补缺项，
重新计算SHA-256并与MANIFEST逐项核对。`git ls-tree -r <参考提交>` 只读取得对象清单，
用原内容计算 Git blob 摘要区分已收录/变化/未收录，没有 add/commit/checkout 等写操作。
规则链接保持相对链接，链接摘要不误当成目标文件摘要。

## 从当前代码得到的状态文件约定

以下只检查源码，不读取宿主 `ctx.paths.data_dir` 或任何真实账本。

### 插件原账本

`delivery_ledger.py` 53行固定 `LEDGER_FILE_NAME="delivery_ledger.sqlite3"`；唯一表
`delivery_ledger`，主键为 `account_id/source_group_id/source_message_id/target_group_id`，
原列为 `status/target_message_id/comment_status/comment_message_id/reason/created_at/updated_at`。
原公共入口 `open_ledger` 直接 `CREATE TABLE IF NOT EXISTS`，尚不能区分缺表历史与新装；
后续第5组必须先检验再建表。原 `recover_interrupted` 只恢复源sending，不能冒称已有评论恢复。

识别约定：

- 完整原库：先独占，核对schema/完整性/状态再兼容扩展；sent/unknown保持阻断，源sending恢复unknown。
- `delivery_ledger.sqlite3-wal/-shm/-journal` 是 SQLite 伴随文件可能形态，不是另一个空库；
  主库缺失但伴随文件存在必须停止。不能删除伴随文件来制造“新装”。
- 损坏库、缺表、未知列/不兼容状态、符号链接到目录外或来源不可确认均关闭发送。
  只读检查失败与不存在严格区分，不将数据库错误吞成None。

### 服务切换收据

`remote_client.py` 41行固定 `service_binding.json`；`read_service_receipt` 只把字段
`ledger_identity/migration_confirmation` 当非敏感绑定事实。770–774行先写
`service_binding.json.tmp` 再原子替换。**正式文件或残留tmp、损坏内容均不是新装证据**；
发现即保留并关闭发送，不能因无法解析就忽略。

### 原库归档与服务导出

当前 `plugin.py/remote_client.py/service/ledger_migration.py` 没有自动重命名原账本或
固定“原库归档文件名”的实现。迁移读取显式 `snapshot`，导出读取显式 `output`，
导出临时名为 `<output>.tmp-<8位随机十六进制>`（`ledger_migration.py` 644行）。
运维文档要求人工保留旧库/快照，**不能编造 `.bak` 或某个固定归档目录已存在**。

因此第5组冻结保守识别：仅在本插件授予目录中识别原库及已知伴随文件；正式/临时收据、
其它快照/归档/导出痕迹或任何无法解释的现有条目均视为历史不明并停止。只看本目录，
不递归扫描其他插件或服务目录、不读取未知文件正文、不自动回迁、不移动或删除证据。
确认新装只接受无历史条目的新目录（不含本次刚取得的锁）；空库缺表不等于新目录。

### 服务端状态（只作排除与误放识别）

`service/ledger_store.py` 52–58行固定 `service_ledger.sqlite3`、`service.lock`、
旧库名 `delivery_ledger.sqlite3`；数据集身份、版本和导入摘要在 `ledger_meta` 中，
服务尝试表为 `delivery_attempts`。插件**不打开/复制真实服务库**。若这些文件被放在
本插件目录，也不能把它当新装，应停止人工核对。

后续本地独占可从服务 `StateLock` 的非阻塞 flock 提取；新锁文件命名和新增评论状态
属于第5组实现，不伪称是原部署已存在文件。存在残留任务风险时不释放锁让新实例接管。

## 第7组操作约束

完整快照在第2–6组任何原文件改写前已固定。后续只能以MANIFEST复核原快照，不能用
改写后的文件反向覆盖基线；活跃需要的规则继续单点复用，专属实现再非删除式隔离。
源码快照不是账本回滚方案，旧归档未真实QQ联验的事实保持不变。
