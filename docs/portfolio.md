# 如何用这个项目说明自己的工程能力

项目定位是个人完成的 coding-agent 平台实验。可以讨论你在公司接触过 multi-LLM 平台的背景，但这个仓库里的贡献和指标只能归属于这个实际项目，不应改写成未发生的公司职责。

## Applied AI 主线

核心问题是：长任务中的代码事实会过期，压缩和模型切换可能放大错误。Forge 把文件证据和测试结果绑定到版本，保留可回读工件，并用相同任务/模型/预算比较 evidence guard 开关。

可以用一个真实轨迹展示：模型读文件→补丁修改→旧证据失效→下一次 context 隐去旧事实→新验证证据绑定新 snapshot。指出差异来自输入上下文的可审计改变，而不是声称自己改造了模型内部推理。

目前有真实的协议、隔离、恢复、数据集资格和 DeepSeek 付费 pilot 证据。12 个诊断任务 × 6 个条件 × 1 次重复共 72 次运行，51 次独立通过。B2 与 guard-off 均为 10/12；这次未证实 guard 的修复率收益。整个 campaign 保守计入预算 $1.630805844，包含 5 个费用待核对运行的完整分配；它不是精确账单。可以讨论负结果、统一输出上限对强模型的约束，以及费用完整性和响应完整性需要分开建模。见 [真实实验报告](validation/deepseek-live.md)。

## 系统工程主线

可以讲清三个不容易靠 SDK 封装解决的窗口：

1. 完整模型结果已收到，但 graph checkpoint 尚未写入：从业务账本复用响应，避免重新采样。
2. 命令或补丁已生效，但数据库尚无结果：查同一 job/receipt 或核验 after-hash；不确定就暂停，不盲目重放。
3. 取消、租约迁移和活跃进程同时发生：新 epoch 不是旧进程已停止的证明，必须保留执行器的实际观察。

Independent review 还发现并推动修复了 partial usage 被当最终费用、提前 `os._exit(0)` 被当测试通过、验证阶段修改原测试、工件归属不一致和总运行时间预算未及时停止等问题。面试时可以拿复现测试与修复依据说明设计如何变化，避免只展示一张功能架构图。

## 本次可补充的运行时故事（2026-09-13）

慢 OTLP collector 会使同步 span export 占用 asyncio 线程，连带影响 worker 心跳和取消。为本地与远端建立独立有界 SDK 队列后，用真实 PostgreSQL/rootless Docker 验证 exporter 持续阻塞时续租和命令取消仍完成；另以相同 24 次 HTTP、固定 50ms collector 延迟和 6 对独立进程测量事件循环延迟。独立审阅进一步复现了 SDK 内部日志泄露与队列警告同步阻塞，修复后保留反例测试。取舍是允许可观测数据丢失，保持业务账本权威，并承认关闭不是硬截止时间。详见 [原始证据和测量边界](validation/telemetry-20260913/README.md)。

## 可使用的英文描述

> Built a Python coding-agent workbench with native OpenAI, Anthropic, and DeepSeek streaming adapters, PostgreSQL-backed execution ledgers, LangGraph recovery, and rootless Docker tool execution.

> Implemented versioned context evidence and independent patch verification; added fault-injection tests for interrupted model streams, persisted-response recovery, partial execution outcomes, and cancellation.

> Evaluated six context/routing configurations across 12 diagnostic repair tasks using 72 live DeepSeek trials, with independent grading, immutable failure records, and a persistent campaign budget ledger.

> Isolated local and OTLP trace export with bounded background queues; verified PostgreSQL lease renewal and Docker command cancellation while a trace exporter remained blocked.

这些描述只陈述已实现并验证的工作。模型结果须带上任务数、配置和一次重复的限制；不要把固定脚本的端到端测试写成“自主解决真实用户任务”，也不要把不同配置混合的 51/72 当作某个产品配置的成功率。

## 尚可验证的研究假设

- evidence guard 的扫描/重读成本是否被更少的版本错误抵消？
- portable compaction 是否在同预算下优于只保留近期历史？
- B3 的收益是否优于固定强模型，而非仅仅来自使用更贵模型？
- 保守 unknown-cost 占用是否过度停止任务，怎样用可审计的账单对账改善？

允许实验给出负结果。一个能说明何时没有收益、为什么失败的项目，比没有原始证据的提升数字更能说明工程判断。
