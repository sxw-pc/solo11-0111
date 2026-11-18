# 用途授权判定台

合成数据产品的分层抽样框可用 `sample-frame` 冻结层、行 ID、预算与先导方差；`sample-manual` 会评估一份人工配额的有限总体方差、层覆盖差、确定样本名单与入样概率，尚不寻找最优配额。示例：`python -m purposegate init --db ledger.db --config fixtures/sample-config.json`，再运行 `python -m purposegate sample-frame --db ledger.db --input fixtures/sample-frame.json` 和 `python -m purposegate sample-manual --db ledger.db --input fixtures/sample-manual.json`。

## 分层抽样优化与独立核验

`sample-optimize --db ledger.db --input fixtures/sample-optimize-variance.json` 在冻结抽样框上按冻结合同选择每层整数抽样数 n_h（1≤n_h≤N_h，总成本≤预算）：`min_design_variance` 最小化总体均值先导设计方差 `Σ (N_h/N)²·S_h²·(1−n_h/N_h)/n_h`，平局先减小层覆盖率极差再按层 ID 字典序；`min_coverage_gap` 最小化层覆盖率极差，平局先最大化总抽样行数再最小化先导设计方差及字典序。全部比较用精确有理数，合同在优化前按原文字节 SHA 冻结。方差目标用预算精确 DP 认证最优值、平局搜索只访问 DP 认证的配额；覆盖率目标用入样率窗口精确扫描。步数预算耗尽时报 `indeterminate` 并给出已证明的上下界，不输出伪最优名单。选定配额后按确定优先序（seed+NUL+行 ID 的 SHA-256）抽出真实行 ID；报告的 n/N 是冻结设计在均匀优先序模型下的事前入样率，固定种子只用于重现这一次名单，已冻结名单事后不再具有随机性。相同请求重放幂等，同 plan ID 异文拒绝；新 frame 只能生成新计划，旧名单与来源不回写。

`sample-verify --db ledger.db --id sample-opt-variance-1` 从冻结 frame 原文、冻结合同和优先序独立重算配额可行性、精确最优目标、名单、入样率与指标，识别伪造更优值、重用行 ID、错用成本或被替换的种子。反贪心 fixture：`fixtures/sample-frame-greedy.json`（三层 N=5、先导方差各 1、成本 1/1/4、预算 13）上逐次边际贪心得 [4,5,1]/[5,4,1] 方差 17/180，而 `fixtures/sample-optimize-greedy.json` 证明全局最优 [2,3,2] 方差 11/135。

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
