# 用途授权判定台

合成微观表匿名化有独立采集/人工方案评估入口：先以 `python -m purposegate init --db ledger.db --config fixtures/anon-config.json` 初始化合成数据集，再以 `python -m purposegate anon-record --db ledger.db --input fixtures/anon-source.json` 冻结表和泛化层级，最后以 `python -m purposegate anon-manual --db ledger.db --input fixtures/anon-manual.json` 评估已选行分组的 k 值、敏感值多样性与信息损失。人工入口仍只评估调用者给出的分组，不搜索最优分组，也不发布真实个人数据。

## 精确最优匿名化（anon-optimize / anon-verify）

`python -m purposegate anon-optimize --db ledger.db --input fixtures/anon-opt-l1.json` 对冻结来源求**可证明最小信息损失**的合规变换。求解器（`purposegate/anon_optimize.py`）在有限行子集上枚举满足 k/l 的候选等价类，再以子集动态规划精确覆盖全部行：允许局部泛化（同一列在不同类可取不同层级），每个源行恰好进入一个类，信息损失为每行各列泛化节点成本之和；同分按冻结行 ID、字段与节点稳定破同。两个内部组若发布相同准标识符元组，按全表实际相等元组合并后再检查 k 与敏感值多样性，不以组 ID 伪装成两个等价类。预算耗尽时返回 `indeterminate` 与已证明的上下界，绝不冒称最优；不存在合规分区时返回 `infeasible`。

持久化结果包含内部 `row_id→发布行` 审计映射（`anon_opt_row` 表）、不含直接 row_id 的实际匿名化行表（保留合成敏感值以便验证多样性）、每个等价类的行数与敏感值种类、逐列损失分解，以及合同和来源字节摘要。`anon-verify --db ledger.db --id <plan_id>` 不信任任何已存结果，从冻结来源与合同独立重算发布表、类大小/多样性、损失与最优目标，可识别伪造泛化节点、偷删行、重复等价类 ID、审计表篡改和次优分组。相同 plan_id 异文请求报 `anon_plan_conflict`，相同来源/合同重放返回同一结果，旧计划不会被新来源静默改写。`fixtures/anon-opt-l1.json` 与 `fixtures/anon-opt-l2.json` 是同一来源下两份事先批准合同（l_distinct 分别为 1、2），最优损失分别为 6 与 14，发布行表实际不同；`anon-source-12.json` 是 12 行三列有限层级的有界运行用例。

完全离线，输入与主体均为合成数据。`python -m pip install . --no-build-isolation` 安装；`python -m purposegate demo` 一条命令初始化 SQLite、导入 fixture、计算、生成 JSON/Markdown，并演示两份合同的最优变换与独立 verify；`python -m pytest -q` 测试。

数据流：SourceFile 原始字节及哈希 → RawEvent → Subject 与 RuleVersion → Run 内的 Decision、Evidence、Issue → 报告；身份冲突和坏行保留为 Issue。相同文件哈希与事件键幂等。

状态：待接收→已校验→已判定/待核；决策和证据与运行状态事务提交。错误码包括 parse_error、identity_conflict、evidence_gap、rule_conflict、execution_failure。只操作显式沙箱。

首版判定能力见 `rules.py`。交付物与用后审计均已实现并有持久状态。

## 交付物与用后审计

`delivery-prepare --db DB --id PKG --request req-1 --values fixtures/delivery-values.json` 在授权运行上冻结精确字段和值摘要；`delivery-release --db DB --id PKG --values fixtures/delivery-values.json --out OUT.json` 核验当前授权和内容后释放文件。未授权字段、同 ID 不同内容、授权运行过期会拒绝；输出缺失使审计无法确认。

`usage-record --db DB --events fixtures/usage-events.jsonl` 保存实际使用的人员、字段、用途、时间；`usage-audit --db DB --id PKG` 复核交付文件和使用时点的授权历史，逐事件给出合规、违规或不确定结论。撤回授权后的使用可使同一交付物的后续审计变为违规，故交付时的允许结论不能代替用后审计。`fixtures/delivery-cases.json` 包含正常、字段冲突和输出丢失场景。交付文件与控制 SQLite 分开，异常中断时审计会将缺失或损坏的输出视为不确定。

## 多主体交付结论

同一请求涉及多个人时，报告的 `allowed_fields` 是所有主体可用字段的交集，`partitions` 逐人说明授权字段与拒绝原因。某一人无授权不会被另一人的许可覆盖；没有共同可交付字段时整单拒绝。字段谱系逐祖先核验，目的树出现环、跨版本字段映射不完整或两旧字段映射同一目标时给出无法判定。授权事件按 UTC 时刻重放，日期视为 UTC 当日零时，带时间的记录必须含偏移；授予与撤回同一时刻出现时撤回优先。报告只包含字段名和证据标识，不输出数据值。

决策的 Evidence 关联请求时刻以前实际参与回放的授权事件；`verify` 从 SourceFile 原始行独立核对事件内容与摘要。报告中的字段名单不能代替授权来源证明。
