# 用途授权判定台

完全离线，输入与主体均为合成数据。`python -m pip install . --no-build-isolation` 安装；`python -m purposegate demo` 一条命令初始化 SQLite、导入 fixture、计算、生成 JSON/Markdown；`python -m pytest -q` 测试。

数据流：SourceFile 原始字节及哈希 → RawEvent → Subject 与 RuleVersion → Run 内的 Decision、Evidence、Issue → 报告；身份冲突和坏行保留为 Issue。相同文件哈希与事件键幂等。

状态：待接收→已校验→已判定/待核；决策和证据与运行状态事务提交。错误码包括 parse_error、identity_conflict、evidence_gap、rule_conflict、execution_failure。只操作显式沙箱。

首版判定能力见 `rules.py`。交付物与用后审计均已实现并有持久状态。

## 交付物与用后审计

`delivery-prepare --db DB --id PKG --request req-1 --values fixtures/delivery-values.json` 在授权运行上冻结精确字段和值摘要；`delivery-release --db DB --id PKG --values fixtures/delivery-values.json --out OUT.json` 核验当前授权和内容后释放文件。未授权字段、同 ID 不同内容、授权运行过期会拒绝；输出缺失使审计无法确认。

释放不是一次状态写入。每次释放先在 `delivery_release` 冻结包身份、授权 run、值摘要、目标路径、待发布临时文件路径以及预期载荷摘要，之后依次写临时文件、提交台账、原子更名、最终确认；每一步都独立持久化。任一窗口进程中断后，`delivery-recover --db DB --id PKG --route {promote_verified,require_reapproval}` 重新计算文件与台账摘要，区分已交付、证据不完整与内容冲突：

- `promote_verified`：仅当待发布文件完整、摘要吻合且授权 run 仍有效时，原子晋升为目标文件并确认交付；目标已有异文时绝不覆盖，授权变化时绝不晋升。
- `require_reapproval`：把待发布文件隔离为 `<out>.pending.quarantine`，包回到需要重新授权的状态，不自动释放任何目标文件。

若更名窗口已完成（目标内容与冻结载荷逐字节一致），两条路线都只做确认，不会回滚已成功写入的交付；已交付的历史包与恢复记录只增不改。恢复记录保存在 `delivery_recovery`，包含依据的台账行、文件摘要、授权状态和操作结果。`--fault-at {prepare,temp,commit,rename,confirm}` 可在 CLI 上复现各窗口中断。

台账里的 `released` 不再单独作为文件已交付的证据：`usage-audit --db DB --id PKG` 只有在目标文件与发布证明同在且可重算一致时才进入授权时点核查，其余情况（文件缺失、被改写、待重新审批、仅状态字符串）一律为不确定。

`usage-record --db DB --events fixtures/usage-events.jsonl` 保存实际使用的人员、字段、用途、时间；`usage-audit --db DB --id PKG` 复核交付文件和使用时点的授权历史，逐事件给出合规、违规或不确定结论。撤回授权后的使用可使同一交付物的后续审计变为违规，故交付时的允许结论不能代替用后审计。`fixtures/delivery-cases.json` 包含正常、字段冲突和输出丢失场景。交付文件与控制 SQLite 分开，异常中断时审计会将缺失或损坏的输出视为不确定。

## 多主体交付结论

同一请求涉及多个人时，报告的 `allowed_fields` 是所有主体可用字段的交集，`partitions` 逐人说明授权字段与拒绝原因。某一人无授权不会被另一人的许可覆盖；没有共同可交付字段时整单拒绝。字段谱系逐祖先核验，目的树出现环、跨版本字段映射不完整或两旧字段映射同一目标时给出无法判定。授权事件按 UTC 时刻重放，日期视为 UTC 当日零时，带时间的记录必须含偏移；授予与撤回同一时刻出现时撤回优先。报告只包含字段名和证据标识，不输出数据值。

决策的 Evidence 关联请求时刻以前实际参与回放的授权事件；`verify` 从 SourceFile 原始行独立核对事件内容与摘要。报告中的字段名单不能代替授权来源证明。
