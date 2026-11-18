# 用途授权判定台

离线合成 cohort 原值与主体更正可通过 `python -m purposegate cohort-snapshot --db DB fixtures/cohort-snapshot.json` 和 `python -m purposegate cohort-corrections --db DB fixtures/cohort-corrections.jsonl` 登记。`purposegate/cohort.py` 只保存原始证据，尚不生成更正后的均值、名次或授权判定。

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

## 更正重述与版本化派生数据

`cohort-snapshot` / `cohort-corrections` 只登记原值快照与更正原文（证据），不直接产生新值。派生重述分三步冻结来源链：

1. `correction-plan --db DB --contract {retroactive_restate|prospective_version} [--run RUN_ID]` 把快照原文、成员集合、指标合同、每条更正原文、授权 run 引用冻结为 `correction_plan`，并逐条核对更正人身份、旧值与当前有效版本、事件键与同字段更正次序。旧值不匹配（`old_value_mismatch`）、同键异文（`correction_event_conflict`）、同一时刻互斥更正（`mutually_exclusive_corrections`）、缺成员（`correction_member_missing`）、无计算定义（`metric_definition_missing`）或来源摘要损坏（`source_digest_corrupt`/`correction_digest_corrupt`）都会把该 cohort 标记阻断，只保留可追溯 issue，绝不发布看似完整的新值。
2. `correction-apply [--plan PLAN_ID]` 对未阻断 cohort 按声明的算术均值（两位小数、四舍五入）与降序密集并列名次重算整个人群，生成不可变 `cohort_version`：每个版本绑定真实贡献者集合（`member_set_json`）、源 `score` 字段、更正事件键、授权 run 与逐贡献者证据摘要。只改 p1 行会遗漏 p2 的名次变化，因此重算始终面向全体成员；多个 cohort 共享 p1 时各自独立闭包重算，名次不跨群组复制。
3. `cohort-query --cohort C --person P --metric {mean|cohort_rank} --at AT` 在冻结合同下解析某时点的权威版本；`correction-verify` 与 `verify` 从快照字节和更正原文独立复算每个版本的均值、名次、状态机与贡献者许可，漂移按 `cohort/成员/事件` 精确定位。

历史效力合同在计划时预先冻结，不能按查询结果事后切换：

- `retroactive_restate`：经核实的更正重述 cohort 原生效日的权威查询结果；旧版本标 `superseded` 但原文与原值保留。p1 由 60.00 更正为 90.00 后，查询更正前时点的 p2 也返回名次 2、均值 85.00。
- `prospective_version`：旧版本在更正登记时点前保持权威（状态 `archived`），新产品版本只从登记时点起生效。更正前查询 p2 返回名次 1、均值 70.00，更正后返回 2、85.00。

请求某人的 `cohort_rank` 或群组 `mean` 时，`evaluate` 解析该 cohort 的冻结版本，并要求版本绑定的**每一个贡献者**在请求目的、源 `score` 字段（沿字段谱系）与请求时点上各自持有授权，再按既有 per-person 分区与交集裁定。请求者本人的许可不能覆盖另一贡献者；任一贡献者证据不足、更正冲突或版本缺失，派生值进入无法判定（`contributor_evidence_gap` 等），旧 score 授权缓存不能让其继续允许。更正前已完成的决策 run 与原始交付字节不回写；新运行与新版本显式引用冻结的成员/更正/授权摘要。`python -m purposegate demo` 在隔离沙箱中演示两种合同的前后时点查询、跨群组闭包、贡献者授权阻断与冲突留痕。
