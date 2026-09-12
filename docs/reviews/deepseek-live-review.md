# DeepSeek live campaign 独立审计

审计时间：2026-09-11 PDT / 2026-09-12 UTC。只读核验本地证据、账本和专用 PostgreSQL 数据库；审阅者未查询或调用模型。**最终 72-trial 矩阵审核通过：51 verified、21 failed。费用仍有 5 个未完全对账的 run；当前总占用 $1.630805844 包含保留的未知额度，不是精确账单。**

## 开发 smoke 与费用

独立读取四份本地 smoke evidence 和持久账本，逐 attempt 根据完整 usage、缓存拆分及冻结峰值费率重算金额。**36 个 attempt 全部 settled，usage 均非 estimated；重算金额逐项与 reservation、slot result 及 retained amount 一致。** 此处是费用上界，不是供应商精确账单。

| 开发运行 | 最终状态 / 独立验证 | 已计费 attempt | 费用上界 USD |
|---|---|---:|---:|
| 原 Flash：16K / 12 次 | budget_exhausted / unverified | 2 | 0.001526112 |
| 第二 Flash：32K / 12 次 | budget_exhausted / unverified | 12 | 0.015467496 |
| 当前 Flash：32K / 24 次 | completed / verified | 10 | 0.011548776 |
| 当前 Pro：32K / 24 次 | completed / verified | 12 | 0.055941160 |
| 合计 | 两次开发失败保留 | 36 | **0.084483544** |

原两个失败 slot 的 run ID、原始定义、失败结果、费用、从 unknown 到 upper_bound 的结算历史仍在账本中。它们的定义与两次 amendment 保存的旧 plan 一致，没有被新开发运行覆盖或重标成功。

## 当前配置与 gate

本地冻结描述为 revision 3；账本保存两条 amendment 历史。重新运行无网络的 `load_plan()` 并计算得到相同 plan hash：`774104da31c343a48c400685683d568ac1d1509ac2d09b73c834c9d6fdfb4e90`。

当前 Flash 和 Pro evidence 中的 manifest config hash 分别与 revision 3 的各自模型配置及对应 slot 定义一致；两者共享 32,000 上下文、24 次模型请求上限。无网络调用 `validate_smoke()`，确认 evidence 为 live、completed、独立 verified、匹配原 run ID、source_unchanged 且 receipts 全部 rootless。

静态计划分配仍为 **$9.44**，其中已保留四个开发 smoke slot 的全部原始上限；授权为 $10，未分配余量 $0.56。最终矩阵逐项匹配 heldout 开始前冻结的 revision 3，没有新增重复或替换样本。

## 公开报告输出边界

只读审查 `src/forge/evals/report.py`：`write_report()` 仅创建 `summary.json`、`trials.csv`、`report.html`。CSV 字段为显式列白名单；汇总与 HTML 只取统计、状态、任务 ID、条件、费用和时间等字段，不序列化嵌套 outcome / grade 全对象。

另执行了无网络的合成 canary 检查：向内存中的 trial 填入可识别的假 patch、诊断消息、private grader 日志、host workspace 路径和 artifact 路径，在临时目录调用原始 writer。结果只有上述三个文件，所有 canary 和假 host 路径均未出现在输出中。没有读取或导出私有参考答案。

原始 `.forge` evidence、完整 trial JSONL 和 grader 日志仍属于本地审计材料，不应作为公开报告直接复制：成功 smoke evidence 本身含候选 patch，原始 grader 记录可含本机路径。

## 最终矩阵与独立判分

逐条核对完整原始 JSONL、ledger、任务 manifest 和六份生成配置，确认恰为 **12 个冻结任务 × 6 个条件 × repeat 0**，所有 seed 为 1729；72 个 trial ID、72 个 heldout run ID 均唯一。各条 base commit、condition hash、slot config hash 与 revision 3 一致。

专用数据库使用只读事务查询，实际恰有 76 个 run，ID 集合与 ledger 完全相同，76 个 idempotency key 各不相同；所有持久 config 的 hash 都与 manifest 和 slot 定义一致。账本最终为 **71 finished、5 uncertain**，没有 pending 或 claimed。这里的 finished 表示计费结算，不等于修复成功；原两个失败开发 smoke 因此仍保留失败状态。

| 条件 | 独立 verified / 12 | 成本未完全对账的 run |
|---|---:|---:|
| B0 | 8 | 0 |
| B1 | 9 | 1 |
| B2 | 10 | 0 |
| B3 | 10 | 0 |
| strong_only | 4 | 4 |
| B2_guard_off | 10 | 0 |
| 合计 | **51 / 72** | **5** |

51 个 verified 原始 grade 均同时记录 target 与 public regression 通过、恰好 2 个 target test、至少 1 个 public test、rootless Docker，以及校验前后相同的 workspace snapshot 且无 tampered paths。修复结果来自独立 grader，不以普通 agent run 的内部完成状态代替：评测配置不把私有 target 交给 agent，其内部 `failed/regression_only` 候选仍须由外部 grader 决定最终质量结果。

原始 JSONL 中的 72 条记录与公开 CSV 逐字段一致；公开 summary 与直接调用 `summarize(raw_trials)` 的重算结果完全相同。上述 ID、幂等键和持久记录检查未发现额外 run、重复 trial、回放或替换。

## 最终费用与解释边界

独立用 Decimal 加总 ledger，结果为：

| 项目 | USD |
|---|---:|
| 已完整结算 run 的峰值费率上界，含全部四个 smoke | 1.030805844 |
| 五个未完全对账 run，各保留完整 $0.12 | 0.600000000 |
| 总预算占用 | **1.630805844** |

五个 unknown run 内已经 settled 的部分请求包含在各自 $0.12 预留内，不再重复加到第一行。此重算与公开 `accounting.json` 一致，`exact_campaign_cost_usd` 为 null；`completed_with_unresolved_cost` 准确区分已完成的质量实验与尚不精确的美元对账。不能把 $1.630805844 称为供应商实际扣款，也不能把 unknown 当成零。

以下限制必须随结果保留：

- 原始 72 条 `stale_current_leaks`、`constraint_losses` 全为 null。每个条件对应的 missing-trials 计数均为 12；聚合中显示的 0 是缺值求和的结果，**没有测得“零泄漏”或“零约束丢失”**。
- 每个条件的 model/retrieval 阶段时间同样缺少 12 条，不能据此给出模型推理或检索阶段的速度提升结论。
- 这是 12 个人工诊断任务、每条件一次运行的探索性比较，不是 72 个独立任务，不是原 540-trial 研究，也不是 SWE-bench。配对区间按任务重采样；不能仅凭该小样本将结果推广到其他仓库或证明模型、路由、证据机制普遍更优。尤其 B2 与 guard-off 本轮相同得分，不提供 guard 效果改善的实验证据。

## 最终公开文件复查

实际检查 `evals/reports/deepseek-10usd/` 中五份文件（summary、CSV、HTML、smokes、accounting）及 `evals/deepseek-live-status.json`，未发现候选 patch 正文、参考答案字段、私有 grader 日志/路径、host workspace 路径或凭据。还将每条原始 trial 的候选 patch 和私有 artifact 路径与公开内容比较，并在内存中检查本地配置的 DeepSeek key 未出现；没有输出 key 或读取私有参考答案。

本次审阅绑定的原始结果 SHA-256 为 `0eab40c8ca55bbea441d8dd79cc84b30c67a5b24430d48aaf80c7a1d1e050345`；最终 ledger SHA-256 为 `bf87715ac5a2ccb41f7a5084eceb593f9c9ddb2f074b97d062b1ecd6bc0277ff`。原始结果与账本未被审阅操作修改。

配套 `docs/validation/deepseek-live.md` 的事实陈述也已交叉核验：21 次失败细分为 9 次 target failure、6 次 agent failure、6 次 budget exhausted；五个 unknown outcome 的原始消息均为 `response.incomplete`。strong-only 的时延中位数四舍五入为 59.18 秒。该文引用的离线 JUnit 确为 254 tests、0 failures、0 errors、3 skipped，即 251 passed；这些历史离线结果不计入 72 次真实 trial。该文对混合条件成功率、预算口径、配对区间退化及未测指标的限制说明与审计结论一致。

## 实验后费用最终性修复复核

本轮暴露的 adapter 问题已针对**未来请求**修复：`response.incomplete` / `response.failed` 的输出不能执行，但其终端事件可以提供完整的最终 usage。源码现要求事件类型与 response status 一致、输入输出计数完整、缓存/推理子集及可选 total 一致，才发出 `UsageUpdate(final=True)`；随后仍返回 ProviderFailure。缺失、部分或不一致 usage 保留未知预留，不产生 ToolCallReady / TurnCompleted，也不触发隐式重试。未配置价格的 cache write 即使 token 已最终确认，仍保留未知金额。

独立运行新增 OpenAI / DeepSeek 合同场景，真实 SDK 配合 MockTransport，得到 **38 passed、48 deselected in 1.23s**；覆盖 incomplete / failed 的完整用量、缺失或部分计数、非法子集 / total、事件状态冲突、未知 cache-write 价格、EOF 和 socket 中断。每个场景都检查一次 HTTP 请求、工具不释放及适当费用占用，未请求真实模型。

另外只读审阅主执行线程新增的三个 PostgreSQL / rootless 运行时回归及其 JUnit：完整终端 usage 结算为费用上界 $0.00006612，partial / missing 保留预留；三者均为单 HTTP、failed run、没有 ToolRow，JUnit 为 **3 passed、0 skipped**。这些运行时测试由主执行线程执行，审阅者未重复执行。

修复之后再次核对上述原始 JSONL 和 ledger 的 SHA-256，均保持不变。原来的 72 个实验结果、五个 unknown 分类及 $0.60 预留不据新代码追溯改写；离线修复验证也没有混入真实实验成绩。
