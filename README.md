# 用途授权判定台

合成授权来源的采集轮询台账可用 `poll-forecast` 冻结逐槽更新概率、预算、每槽容量与来源，再用 `poll-manual` 评估人工时序，或用 `poll-optimize` 求全局最优计划、`poll-verify` 从冻结原文独立复核；不访问网络。示例：`python -m purposegate init --db ledger.db --config fixtures/poll-config.json`，然后 `python -m purposegate poll-forecast --db ledger.db --input fixtures/poll-forecast.json`，接着 `python -m purposegate poll-optimize --db ledger.db --input fixtures/poll-opt-request.json`，最后 `python -m purposegate poll-verify --db ledger.db --input fixtures/poll-verify-request.json`。人工评估仍由 `poll-manual` 保留。

`poll-optimize` 的输入固定两份目标合同之一：`min_expected_stale` 先最小化全源权重乘逐槽陈旧概率之和、再最小化最大未轮询间隔；`min_max_gap` 次序相反，并列时按槽序与源 ID 序稳定破同分。输出真实的源×槽轮询指令、逐槽 `q` 的精确分数、累计暴露、各源最大间隔、使用预算、来源摘要与全局最优性证据（DP 转移数、状态数、破同分规则）。求解是保留各源上次轮询槽与已用预算的有限时域精确 DP，非贪心；预算或容量不足以让所有源在末槽封账时返回 `infeasible` 及原因，转移预算耗尽时返回 `indeterminate` 与已证上下界，不冒称最优。`poll-verify` 重算逐槽 `q`、资源约束与目标最优性，可识别漏轮询、乘积错算、概率篡改和次优替换。

完全离线，输入与主体均为合成数据。`python -m pip install . --no-build-isolation` 安装；`python -m purposegate demo` 一条命令初始化 SQLite、导入 fixture、计算、生成 JSON/Markdown；`python -m pytest -q` 测试。

数据流：SourceFile 原始字节及哈希 → RawEvent → Subject 与 RuleVersion → Run 内的 Decision、Evidence、Issue → 报告；身份冲突和坏行保留为 Issue。相同文件哈希与事件键幂等。

状态：待接收→已校验→已判定/待核；决策和证据与运行状态事务提交。错误码包括 parse_error、identity_conflict、evidence_gap、rule_conflict、execution_failure。只操作显式沙箱。

首版判定能力见 `rules.py`。交付物与用后审计均已实现并有持久状态。

## 交付物与用后审计

`delivery-prepare --db DB --id PKG --request req-1 --values fixtures/delivery-values.json` 在授权运行上冻结精确字段和值摘要；`delivery-release --db DB --id PKG --values fixtures/delivery-values.json --out OUT.json` 核验当前授权和内容后释放文件。未授权字段、同 ID 不同内容、授权运行过期会拒绝；输出缺失使审计无法确认。

`usage-record --db DB --events fixtures/usage-events.jsonl` 保存实际使用的人员、字段、用途、时间；`usage-audit --db DB --id PKG` 复核交付文件和使用时点的授权历史，逐事件给出合规、违规或不确定结论。撤回授权后的使用可使同一交付物的后续审计变为违规，故交付时的允许结论不能代替用后审计。`fixtures/delivery-cases.json` 包含正常、字段冲突和输出丢失场景。交付文件与控制 SQLite 分开，异常中断时审计会将缺失或损坏的输出视为不确定。

## 多主体交付结论

同一请求涉及多个人时，报告的 `allowed_fields` 是所有主体可用字段的交集，`partitions` 逐人说明授权字段与拒绝原因。某一人无授权不会被另一人的许可覆盖；没有共同可交付字段时整单拒绝。字段谱系逐祖先核验，目的树出现环、跨版本字段映射不完整或两旧字段映射同一目标时给出无法判定。授权事件按 UTC 时刻重放，日期视为 UTC 当日零时，带时间的记录必须含偏移；授予与撤回同一时刻出现时撤回优先。报告只包含字段名和证据标识，不输出数据值。

决策的 Evidence 关联请求时刻以前实际参与回放的授权事件；`verify` 从 SourceFile 原始行独立核对事件内容与摘要。报告中的字段名单不能代替授权来源证明。
