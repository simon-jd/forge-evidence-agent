# DeepSeek $10 campaign：独立预算设计审查

审查日期：2026-09-11。范围：只读既有运行时、计费和评测实现；本文件提出实现与验收要求，不代表新实现已通过审阅。没有发起付费模型请求。

## 结论与固定实验范围

DeepSeek 可用于本轮真实评测。用户已授权本轮全部 API 费用合计不超过 USD 10；该额度应覆盖公开演示、正式 trial、失败、重试和中断后残留计费。

建议在发出首个请求前冻结以下清单、配置散列与任务 ID，不根据中途成绩追加或替换样本：

| 工作 | 数量 | 每个 run 的上限 | 合计预留 |
|---|---:|---:|---:|
| 独立公开演示，Flash / Pro 各一次 | 2 | $0.20 | $0.40 |
| 12 个 heldout 任务 × B0/B1/B2/B3/strong_only/B2_guard_off × 1 次 | 72 | $0.12 | $8.64 |
| 已规划工作合计 | 74 | — | $9.04 |
| 用户上限以内未分配余量 | — | — | $0.96 |

12 个任务须在查看 live 结果前按固定种子、任务类别分层选择并落盘。保留全部失败、预算停止和基础设施失败。此轮是小样本诊断实验，不是原计划 3 次重复的完整实验，也不能报告 SWE-bench 成绩或宣称已证明方法领先。未分配余量不自动触发更多实验。

## 价格依据

本次直接读取的官方价格为每百万 token：Flash 未缓存输入 $0.30、缓存输入 $0.006、输出 $1.20；Pro 分别为 $1.32、$0.044、$3.96。这是高峰价格；工作日 UTC 01–04、06–10 以外为半价。型号为 `deepseek-flash` 和 `deepseek-v4-pro`。因此冻结高峰价格适合保守预算，但不能把高峰价乘 usage 称为精确账单支出。[官方价格页](https://api-docs.deepseek.com/quick_start/pricing/)

模型别名、价格、读取日期及其来源必须写入 campaign / run manifest。公开结果另保留 provider 返回的实际模型 ID。若重新开始前价目变化，先核验冻结费率仍为上界；禁止自动提高授权总额。

## 已定位的缺口与最小改法

1. **P1：总额只在一个 Python 调用中有效。** `evals/harness.py` 的 `committed = Decimal(0)` 不覆盖两次公开演示，不跨进程重启，也不能防止两个 evaluator 同时消耗各自的总额。新增持久 campaign 与 run reservation；所有本轮 paid run 在进入队列的同一事务中，锁定 campaign、检查剩余额度并预留整个 run 上限。使用唯一 `(campaign_id, logical_run_key)`，复用同一键必须返回同一 run，参数散列不匹配须拒绝。campaign 的 owner、总额和已冻结计划不能在重启时重置。

2. **P1：未知费用必须继续占用额度。** SDK 发请求前现有 `BudgetManager.reserve` 已持久化，是应保留的基础。连接中断、usage 缺失、失败重试、worker 崩溃均不能记零；未确认 run 保留其 run reservation。终止且无未知 attempt 的 run 可按已确认费用上界释放多余预留；终止但 usage 未知则保留完整 run 上限。不能以发出取消请求或 evaluator 超时作为已停止计费的证明。回收和终止事务需要检查 lease / epoch，避免旧 worker 回写或提前释放。

3. **P1：预留输入必须覆盖实际提交内容。** `runtime/engine.py` 目前使用 ContextBundle 的 canonical JSON 字节估计。provider 转换后新增、复制或重新发送内容可能改变输入量。建议给 provider 一个无网络的实际请求预算估计，按最终 messages、tools、native 内容的 UTF-8 字节及明确 framing 余量计算，runtime 使用其与现有估计的最大值。本轮明确 `portable`、纯文本、关闭 thinking，不启用未纳入计数的附件和 native reasoning 重放。字节量仍是保守工程估计，不是官方 tokenizer 的精确计数；对账发现 usage 超出预留应记录超限、停止 campaign 并保留差额，不能将其截断到预算数字。

4. **P2：完整 usage 不等于精确美元支出。** `BudgetManager.reconcile` 在 token 完整后标记 `settled`，worker 再直接输出 `cost_status=exact`。最小兼容改法是 `ModelSpec.price_basis: exact | upper_bound`（默认 exact），保留 settled 作为 usage 对账状态；新 DeepSeek 配置选择 upper_bound。`AgentTrialOutcome` 与 `EvalTrial` 增加 `upper_bound` 成本状态。任何 settled 费用使用上界费率，则整体是 upper_bound；存在未知 attempt 时整体仍为 unknown。不要把 price_basis 放进 `TokenUsage.estimated`，那会混淆 token 和价目两个不同的不确定性。

5. **P2：report / harness 必须理解上界。** 完整 usage、零未知占用且 cost_status 为 exact 或 upper_bound 时，都能判断是否已证明在预算内。报告分别呈现 `cost_exact_usd`、`cost_upper_bound_usd`、`upper_bound_cost_trials`、未知占用。存在 upper_bound 时精确总价及精确每成功成本应为 null；若提供每成功成本上界，需明确标注。不要把上界计为未知费用，亦不要把未知费用记零。

## 请求与对账约束

- 每个 SDK 请求必须显式传 `max_tokens`；官方 thinking 默认开启，默认输出上限也远高于本轮预算，所以不能依赖服务端默认值。SDK 内置自动重试应关闭，由现有持久 attempt 层计数及逐次预留。[请求参数文档](https://api-docs.deepseek.com/api/create-chat-completion)
- 总输出 usage 已包含 reasoning 子集，不能重复加 reasoning tokens。缓存缺少拆分时按未缓存价格占用；含缓存拆分时先验证其不超过总输入。
- 最终 SSE 帧同时有非空 choices、finish_reason 和 usage；不要只等待 OpenAI 风格独立 usage-only 帧。本轮应对实际 DeepSeek 帧形状写 contract test。[流式 usage 定义](https://api-docs.deepseek.com/api/create-chat-completion)
- request 预留属于 run 预留内部，campaign 不应把它重复计入总额。公开演示和正式实验使用同一 campaign；任何独立 smoke 请求同样必须先登记并从该额度扣除。
- 每个已发生的付费 run 保留实际完成状态。预算耗尽意味着按计划停止剩余请求，不能通过降成本后重试同一 heldout 样本来替换首次结果。

## 发起 live 请求前的验收

| 场景 | 必须观察到的行为 |
|---|---|
| 两个事务同时预留最后一笔额度 | 至多一个成功；余额和记录在 PostgreSQL 中一致 |
| 相同 run / trial key 重试、进程重启 | 复用原 run 和 reservation，不再次付款、不重置 campaign |
| 已发送请求后断流或进程崩溃 | 未知占用保留；后续请求因剩余额度不足而停止 |
| 429 / 5xx 后应用层重试 | 每次都有持久 attempt；SDK 不隐式重试 |
| 最终 usage 完整、使用峰值价格 | 标记 upper_bound；报告不出现伪精确账单 |
| 缺 usage、缓存拆分非法或 token 超估算 | 不释放不确定额度；超限数据可见且停止新增请求 |
| 请求含长中文、多工具 schema / result | 最终 wire 内容全部参与预留；max_tokens 明确存在 |
| 非活动 worker 尝试结算旧 lease | 拒绝旧状态写入及额度释放 |
| 两个公开演示加 72 个 trial | 同一 campaign、固定清单，计划预留 $9.04；总授权不可超过 $10 |

以上是实施前的独立审查；实现后的测试与 live 账单证据应另行记录，不将本文件作为其通过证明。

## 2026-09-11 实现复核

复核对象为本轮未提交工作树中的 `campaign.py`、`deepseek_campaign.py`、计费和报告改动。新实现采用单机文件锁、原子替换与目录 fsync 的持久 campaign 账本，而不是本文件起初建议的 PostgreSQL 总表。对这次固定单机命令，该实现能保证同一 slot 不被两个进程重复领取，且崩溃后未确认预留不会自行重置。其授权范围是 `forge.evals.deepseek_campaign` 固定命令；它不是对所有 Forge API 或独立 demo 命令的全局账户限额。此次授权的付费操作必须全部从该命令进入。

实际选择的 provider 协议是 DeepSeek **Responses API**，使用 `max_output_tokens` 和 `reasoning.effort=none`；此前关于 Chat Completions 的 `max_tokens` 和末帧结构属于备选协议说明，不能直接套用于当前 adapter。adapter 已添加最终 wire JSON 的字节预算估计与 framing 余量。峰值费率已使用 `price_basis=upper_bound`，报告分别保留精确价格、价格上界及未知费用。

独立新测试位于 `tests/review/test_deepseek_budget.py`，全部通过 mock 代替数据库、Docker 和 HTTP，不产生 API 费用。

| 复核场景 | 首次复核结果 |
|---|---|
| smoke 子进程超时 | 通过：slot 保留 $0.20 未知占用，后续 73 个 slot 仍 pending |
| smoke 子进程非零退出 | 通过：slot 保留 $0.20 未知占用，停止启动后续工作 |
| run 处于 needs_reconciliation 且暂时没有 usage | **失败：callback 返回 exact $0，导致 campaign 可提前标记 finished 并释放占用** |

最后一项是本次已修复的 P1 计费一致性问题：`ServiceRunCallback` 原先根据已有 attempt 是否完成推断成本，却没有先确认整个 run 已最终结束。固定 74 个 slot 的静态分配仍将计划上限限制在 $9.04，因此首次复现证明的是提前结算和错误报告，未证明实际支出超过 $10。

首次复核结果为 **1 failed, 2 passed**；保留上表说明问题及回归来源。修复后独立确认：代码先检查整个 run 的状态是否属于 `TERMINAL`；非终止 run 强制输出 unknown，未知占用取现有未知 attempt 金额与「run 上限减已知费用」的最大值，并将总体 usage 标为 estimated。

追加的独立场景保留一个已完成且已对账的 earlier attempt：10,000 输入、1,000 输出，峰值价格上界 $0.0042。即使此前 attempt 均为 settled，暂停的整个 run 仍保留 $0.1158 未知占用，合计 $0.12；已报告的 token 与已知费用不丢失，也不重复计费。另一个场景确认完全没有 usage 时保留完整 $0.12。

修复后复验命令：`timeout 30 .venv/bin/pytest tests/review/test_deepseek_budget.py -q --tb=short` → **4 passed in 0.24s**；该测试文件的 Ruff 检查通过。本轮未使用 PostgreSQL、Docker 或真实模型；所定位 P1 已关闭，预算 guard 的真实模型/实际账单验证仍应由 live campaign 单独出具。

## 首次开发 smoke 失败后的修订设计审查

主执行线程报告：首个 Flash 开发 smoke 完成两次模型请求后，由于 mandatory context 加输出预留为 16,147、超过配置的 16,000，在第三轮请求前停止。原 run 为 `fde03862f6104d32a9f239422c6a939d`，上界费用为 $0.001526112；其余 73 个 slot 尚未启动。以下是基于该执行证据的修订设计评审；审阅者未调用模型。

可以保留原始失败记录后，将尚未启动的工作改为 32,000 的上下文预算，并新增**唯一一个** $0.20 的 Flash 开发 smoke。从原未分配的 $0.96 中划拨后，计划总分配为 **$9.24**，未分配余量为 **$0.76**，用户授权仍为 $10。这是开发 smoke 暴露的配置修复；不能将原失败删除、重标为成功，或将新增执行当成原 run 的重试。当前没有 heldout 结果，修改尚未启动的实验配置不构成挑选 heldout 成绩。

修订必须通过显式账本操作，而非修改 JSON 后直接重跑：

1. 先依据原 run 已终止和两个 attempt 最终 usage 的证据，结算原 slot，保留其失败原因、run ID 和原始日志；不能用“发出了取消请求”作为结算证明。
2. 在与 claim 相同的文件锁中，对旧 `plan_hash` / revision 做比较；任何 heldout 已启动，或任意 slot 仍为 claimed / uncertain，均拒绝修改。
3. 原 finished slot 的定义、run ID、结算与失败证据保持不变。所有原 slot 继续存在；授权额、dataset hash、原 slot 的种类和金额均不能变化。只允许改尚未启动 slot 的配置，以及增添事先指定的新开发 smoke。
4. 历史记录保留旧完整 plan、旧 hash、新 hash、revision、修订原因及所依据的原开发 run ID；所有金额仍满足静态分配不超过授权。
5. 恢复命令跳过已完成的旧失败开发 run，执行新 Flash 与原 Pro 的当前 32,000 配置 smoke。**必须检查两者明确的独立 verification=verified 证据及配置 hash，不能只检查 slot state=finished。** finished 只表示计费结算，也包括旧失败 smoke。

实施后的独立验收应覆盖：

| 场景 | 应有结果 |
|---|---|
| 一个已结算失败开发 smoke、其他任务 pending | 允许唯一指定修订，旧 run / 金额 / 失败证据保留 |
| 任一 heldout 为 claimed、uncertain 或 finished | 拒绝修订，即使其费用为零 |
| 任一开发 smoke 仍 claimed / uncertain | 拒绝修订，不能遗忘在途费用 |
| 请求删除 slot、改授权额 / dataset / 旧 finished 配置 | 全部拒绝，账本不变 |
| 两个调用使用相同旧 revision 同时修订 | 至多一个成功；另一个因旧版本不匹配而拒绝 |
| 新修订重启后再次提交 | 不新增第二个 Flash slot、不重置已启动状态 |
| 旧失败 smoke 已 finished、新 smoke 尚未 verified | 禁止启动 heldout |
| 当前配置的新 Flash、Pro 均 verified | 仅启动原固定 heldout 清单，每 slot 最多一次 |

修订实现已独立复核：`CampaignLedger.amend_plan` 在文件锁内校验旧计划、冻结身份和状态约束，写入历史记录后才更新内存中的 plan。独立新增 14 个参数化回归验证上述账本约束，包括原失败开发 run 的金额、证据及定义保留，违规修改不改变账本字节，零成本但已结束的 heldout 同样阻止修订，以及 stale handle / no-op 拒绝。

连同此前 4 个计费与子进程回归，执行 `timeout 30 .venv/bin/pytest tests/review/test_deepseek_budget.py -q --tb=short` 得到 **18 passed in 0.32s**。此次测试没有运行真实模型、PostgreSQL 或 Docker。

另外只读检查恢复 gate：`validate_smoke` 确认 live execution、completed、独立 verified、当前配置 hash、非空 run ID、源文件未变和 rootless receipts；复用已完成的当前 smoke 时还核对原 run ID。`check_resume` 拒绝未对账状态及任何已启动 heldout。历史失败 Flash 不在当前 smoke 映射内，因此不会被当作新的 32,000 配置成功证明。此处属于源码审阅，新的真实 smoke 和 heldout 成绩仍须由随后执行的 campaign 单独报告。

## 第二次开发配置修订：模型请求次数上限

主执行线程提供的新证据：32,000 上下文配置下的 Flash 开发 run `31ddb30fe23d4900940481a7455a9c42`，12 个 attempt 的 usage 均已 settled，上界费用 $0.015467496。模型曾产生多次 diff 格式/路径错误，随后成功应用补丁，完成 3 个 focused 测试和 6 个完整测试，但尚未发出 finish 时，被 12 次模型请求上限停止。该开发 run 仍应记录失败，不把测试已通过等同于完整 agent 流程成功；尚未启动 heldout。

**接受在原账本中执行第二次显式开发修订：** 保留此前两个失败 Flash slot 及其原定义、run ID、日志和费用，将仅 pending 配置的 `max_model_turns` 从 12 改为 24，并新增唯一开发 slot `smoke-deepseek-flash-context32k-turn24`，上限 $0.20。未启动的 Pro 和所有 heldout 使用同一修订配置；上下文 32,000、每 run 美元上限、输出 4,096、工具次数 40 均保持不变。

静态分配因此为 **$9.44**，用户 $10 授权中的未分配余量为 **$0.56**。24 是模型请求次数上限，不保证一定能请求 24 次；每个 attempt 仍须先通过持久美元预留，美元上限可以更早终止 run。两次旧开发 run 的上界费用合计 $0.016993608，必须继续留在整个 campaign 的费用统计中，不能因为新增开发 slot 而归零。

该修订基于公开开发样例的可观测流程边界，没有查看或替换 heldout 成绩，也没有修饰模型产生的错误补丁。新的当前 Flash 与 Pro 均须完成独立 verified gate，之后才能启动原 heldout 矩阵；最终报告应展示两次开发失败及 heldout 启动前冻结的配置。沿用已验证的显式 amendment 机制，本次只补充设计审阅记录，不重复其既有测试，也不代表新的 live 执行已成功。
