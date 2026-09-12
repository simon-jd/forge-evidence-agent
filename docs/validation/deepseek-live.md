# DeepSeek 真实评测结果

2026-09-11 PDT 完成 `deepseek-10usd-20260911-v1`：12 个预选 heldout 诊断任务 × 6 个条件 × 1 次重复，共 **72 次真实模型 trial，51 次独立验证通过、21 次失败**。两个当前配置的 Flash / Pro 公开 smoke 均通过；前两次开发 smoke 失败也保留在费用与历史中。所有 heldout trial 使用同一份修订后冻结配置，没有重跑或替换。

这是项目自建诊断集上的小规模 pilot。72 是不同条件下的运行数，独立任务只有 12 个；不能把混合配置下的 51/72 当作一个部署方案的成功率，也不是 SWE-bench 成绩。

## 条件比较

| 条件 | 技术配置 | 独立通过 | 运行时中位数 | 完整条件费用上界 USD |
| --- | --- | ---: | ---: | ---: |
| B0 | Flash，近期历史 | 8/12 | 33.76 s | 0.112716864 |
| B1 | Flash，版本化证据 | 9/12 | 25.59 s | 待核对 1 个运行 |
| B2 | Flash，版本化证据与结构化压缩 | 10/12 | 20.82 s | 0.109856508 |
| B3 | B2，可有限升级至 Pro | 10/12 | 29.10 s | 0.151473996 |
| strong_only | 固定 Pro，证据与压缩 | 4/12 | 59.18 s | 待核对 4 个运行 |
| B2_guard_off | B2，关闭过期证据过滤 | 10/12 | 24.37 s | 0.099973068 |

表中费用包含各条件的成功与失败运行，不包含开发 smoke。费用不完整的条件保留空值，不把已知部分当作总费用。时延来自同一台机器上的顺序执行，包含运行队列与本地工具过程，不是厂商纯推理性能。

B1 → B2 在 12 个任务中多通过 1 个；任务配对 bootstrap 的 95% 区间为 0 到 25 个百分点。样本和重复次数不足以建立稳定收益。B2 与 guard-off 的通过任务完全相同，观察到的成功差为 0；退化为 `[0, 0]` 的 bootstrap 区间仅反映这 12 对观测，不能证明机制等效或没有价值。

强模型条件受到统一 4,096 输出 token 上限、$0.12 单运行预算和当前适配器行为的共同约束。其 4/12 不能解释为 Pro 的一般编码能力低于 Flash。B3 与 strong-only 的差异也不足以单独归因于路由策略。

## 预算核对

| 项目 | USD |
| --- | ---: |
| 原始用户授权 | 10.00 |
| 76 个运行的静态最高分配 | 9.44 |
| 没有分配的授权余量 | 0.56 |
| 完整结算运行的峰值费率上界，含四次开发 smoke | 1.030805844 |
| 5 个费用未最终确认运行的完整分配 | 0.60 |
| **本轮保守计入预算合计** | **1.630805844** |

5 个待核对运行按各自完整 $0.12 保留占用，已经包含其内部已知费用，不重复加算。这一合计低于 $10；它是冻结峰值费率估算加保守预留，**不是供应商精确账单**。余额快照和凭据只留在本地忽略目录，报告不公开账户余额。没有用剩余预算追加实验。

这 5 个运行都因 `response.incomplete` 失败；工具未获准执行不完整响应。终端事件已有 token 用量，但本轮执行时的适配器把该用量归为 estimated，导致费用继续占用。完整原始终端 SSE 未持久化，因此保持原始 unknown 分类。实验结束后已修复后续请求的终端费用归类；真实 PostgreSQL/rootless 运行时回归确认完整用量可结算，部分/缺失用量继续预留，三种失败响应都不执行工具。[三项回归证据](deepseek-incomplete-runtime-junit.xml) 不属于付费 trial。参见 [费用分类复盘](deepseek-incomplete-usage.md)。

## 失败与测量缺口

持久化模型账本记录了 823 次 heldout 请求尝试，其中 815 次响应完成、8 次失败；失败不等于免费。完成响应返回的模型名为 `deepseek-flash`（681 次）和 `deepseek-v4-pro`（134 次），均为别名，不能据此证明不可变的底层模型 checkpoint。

21 次失败包括 9 次独立目标测试失败、6 次代理失败、6 次预算耗尽；没有 infrastructure failure 或缺跑样本。5 次代理失败与上述终端截断有关，另 1 次代理失败保留自己的原始错误。模型声明完成与私有目标验收是不同阶段，回归测试通过不保证修复了目标缺陷。

这轮没有采集 stale-current-leak 和 constraint-loss 指标；所有条件对应的 missing-trial 计数都是 12。汇总中的数值 0 不能解读为“测得零泄漏”。模型与检索阶段的独立计时也未采集。后续研究需要补足这些观测和重复样本，不能从通过率反推机制原因。

## 复查入口

- [逐 trial HTML 报告](../../evals/reports/deepseek-10usd/report.html)、[CSV](../../evals/reports/deepseek-10usd/trials.csv)、[JSON 汇总与配对结果](../../evals/reports/deepseek-10usd/summary.json)。
- [完整预算口径](../../evals/reports/deepseek-10usd/accounting.json)、[四次开发 smoke](../../evals/reports/deepseek-10usd/smokes.json)、[机器状态](../../evals/deepseek-live-status.json)。
- [独立结果审计](../reviews/deepseek-live-review.md)、[两次开发修订的原因](deepseek-development-lessons.md)、[冻结实验方案](../evaluation-deepseek.md)。

公开报告由 `forge.evals.report.write_report` 从原始 trial 生成，CSV 与 HTML 使用显式字段投影。候选 patch、私有目标、参考 patch、grader 路径及会话日志保留本地。原始 ledger 和 JSONL 不覆盖，核对依据是 trial/run/config hash 以及原始计划的修订历史。

实验配置修订后的历史离线回归为 [251 passed、3 skipped](deepseek-amendment-offline-junit.xml)。加入终端费用修复后，最终离线组为 **289 passed、3 skipped**，Ruff 通过，mypy 在 57 个文件中通过；另有 3 项真实 PostgreSQL/rootless 运行时回归全部通过。三个离线跳过项需要显式 Docker 审阅环境，不被算作通过；真实 smoke 与 heldout 验证另有 rootless 运行证据。见 [最终离线 JUnit](deepseek-final-offline-junit.xml)，不与历史测试数重复累加。
