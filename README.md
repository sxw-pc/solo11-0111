# 用途授权判定台

合成授权决定的群体审计台账可用 `group-audit-record` 冻结分层计数，并由 `group-audit-raw` 输出两组未经分层调整的允许率及差值。示例：`python -m purposegate init --db ledger.db --config fixtures/group-audit-config.json`，再运行 `python -m purposegate group-audit-record --db ledger.db --input fixtures/group-audit-matrix.json`，最后运行 `python -m purposegate group-audit-raw --db ledger.db --id audit-S1`。`purposegate/group_audit.py` 不改变任何人的许可；raw 摘要不作共同参考人口标准化或分层精确检验。

## 群体审计调查判定

`group-audit-decide --db DB --contract CONTRACT.json` 在给出任何结论前冻结估计合同：审计源摘要、batch/rule 版本、项目调查门槛、群体与层顺序、以及调用者选定的总体定义。`observed_mix` 按各组本次源数据实际层构成加权；`common_reference` 按合同给定的共同层权重（合计为 1 的精确非负有理数）加权两组各层允许率。冻结结果保存每层人数、允许率、权重贡献、两组总体率、精确差值与使用的总体定义，层构成效应不会被误称为层内许可差异。

调查证据为双侧精确条件检验：`observed_mix` 在固定两组样本量与全体允许数边际下使用单一超几何分布；`common_reference` 在每层固定边际下使用独立超几何分布并卷积加权率差。p 为零假设下 `|T|≥|T_observed|`（含等号）的概率，用整数组合数/有理数精确计算，不用高斯近似或随机模拟，不先平均各层 p，阈值比较前不四舍五入。触发调查要求 |允许率差|≥1/5 且精确 p≤1/20，两者缺一即不触发并说明哪项不足。合同限 2–6 层、每层两组总数≤200，超限报明确业务错误，不静默改用近似 p。任一组在某层零样本、层重复/缺失、权重非法、规则版本不符或同一来源 ID 异文时，不给确定调查结论。

`group-audit-verify --db DB [--id CONTRACT]` 从冻结的合同与来源原文重算原始/标准化率、条件分布与调查判定，识别对计数、权重、p 或调查状态的篡改；同一合同重放幂等，同 ID 异文拒绝，旧诊断不被新来源覆盖。结论仅适用于这份离线合成汇总，不声称审计真实人员或法定公平性。

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
