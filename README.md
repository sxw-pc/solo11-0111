# 用途授权判定台

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

## 主体委托链

请求可在 `subjects.<请求>.delegatees` 中声明 `{数据主体: 最终使用方}`。声明后该主体的许可不再走直接授权，而由 `timeline.resolve_delegation` 沿有向委托链裁定；未声明的主体仍走原有直接授权路径，旧请求结果不变。直接授予与委托证据彼此独立：委托链不会回退到直接授权，直接授权也不会借助委托事件。

事件类型（全部为合成离线输入）：

- `delegate_source`：数据主体向第一受托方签发，携带 `purpose`、`fields`、`from`/`until`、`revocation_effect`（`immediate` 或 `fixed_term`）、`delegate_allowed`。
- `delegate`：受托方转授下一级，`source_event_key` 指向直接父授权，`delegator` 必须是父授权的 `delegatee`。可带 `irrevocable_until` 声明不可撤回期限。
- `delegate_revoke`：`delegator` 撤回对 `delegatee` 的委托，同一时刻撤回优先。

每级都核对签发者身份、来源事件（内嵌 `source` 必须与父授权正文一致）、时刻不早于父签发、字段为父字段子集、目的为父目的祖先、期间落在父期间内、以及半开有效期 `from <= 请求时刻 < until`。同一最终使用方的并行链各自独立求交，绝不把两条链的字段并成一份许可：可行链字段集不一致时整单无法判定。

撤回合同取自 `delegate_source` 并在求值时冻结，子事件不能改写：

- `immediate`：父授权撤回后所有子委托立即失效。
- `fixed_term`：撤回前已签发、且声明了 `irrevocable_until` 的子委托在期限内继续有效；撤回后不得再签新的一级委托，受保护的子委托也不得再转授。

缺父授权、环、同一 `event_key` 异文、来源不匹配、超范围或目的扩大、同刻撤回均进入拒绝或无法判定，并在 `delegation_chain`/`delegation_link` 中保留每一级事件、来源 SHA、字段交集与撤回效力；报告 `delegation_chains` 与 `delegation-report` CLI 会列出这些证据。证据按 run 冻结，重新导出不会改写历史 run。

`fixtures/delegation-config.json` 与 `fixtures/delegation-events.jsonl` 提供两种撤回合同、多级转授、字段与目的收窄、并行链、冲突、环与同刻边界等场景；`tests/test_delegation.py` 覆盖这些路径。
