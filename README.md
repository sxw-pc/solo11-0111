# 用途授权判定台

合成双提供方清单的候选实体链接可用 `link-record` 冻结左右记录和两份独立评分声明，再用 `link-manual` 评估人工指定的一对一完整配对。示例：`python -m purposegate init --db ledger.db --config fixtures/link-config.json`，再运行 `python -m purposegate link-record --db ledger.db --input fixtures/link-manifest.json` 和 `python -m purposegate link-manual --db ledger.db --input fixtures/link-manual.json`。`purposegate/matching.py` 的 `evaluate_manual` 只评价人工给出的映射，不寻找最优配对，也不把链接结果当成任何人的授权。

## 规范实体配对证书

`link-decide --db DB --input PLAN` 在冻结清单上按治理者事先选定的复核合同精确求解完整一对一配对：`max_total_score` 先最大化保守分（每边取其全部独立声明的最小分值）总和再最大化最低边分，`max_min_pair_score` 先最大化最低边分再最大化总分，仍并列按冻结 left/right ID 序破同分。证书给出规范实体键（仅由冻结清单摘要与配对身份派生的本地合成 ID）、每对真实 left↔right 的 claim ID/原分/保守分、总分、最低分、来源摘要与全局最优性证据（有限位掩码 DP，最多 12×12 记录与 80 候选边）。无完美匹配时给出可复核的最小阻断割（霍尔违例），预算耗尽时报 `indeterminate` 与已证上下界，不把可行映射冒称最优。`link-verify --db DB --id CERT` 从冻结原始 manifest 独立重算候选图、保守分、完整匹配与目标最优性，识别伪造 claim、重复右记录或次优映射。示例计划见 `fixtures/link-decide-total.json` 与 `fixtures/link-decide-min.json`：同一验收图上两合同的真实配对全部不同（直连 25/5 对循环 24/8）；`fixtures/link-greedy-manifest.json` 证明逐边最高分贪心只得 10 而全局交叉配对得 16。配对仅是合成记录的实际对应关系，不创建联合产品，不构成任何人的授权，不接入授权判定、交付或用后审计。

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
