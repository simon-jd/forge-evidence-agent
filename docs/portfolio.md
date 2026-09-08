# 如何用这个项目说明自己的工程能力

项目定位是个人完成的 coding-agent 平台实验。可以讨论你在公司接触过 multi-LLM 平台的背景，但这个仓库里的贡献和指标只能归属于这个实际项目，不应改写成未发生的公司职责。

## Applied AI 主线

核心问题是：长任务中的代码事实会过期，压缩和模型切换可能放大错误。Forge 把文件证据和测试结果绑定到版本，保留可回读工件，并用相同任务/模型/预算比较 evidence guard 开关。

可以用一个真实轨迹展示：模型读文件→补丁修改→旧证据失效→下一次 context 隐去旧事实→新验证证据绑定新 snapshot。指出差异来自输入上下文的可审计改变，而不是声称自己改造了模型内部推理。

目前有真实的协议、隔离、恢复和数据集资格证据。真实模型修复率、成本收益、路由收益尚未测量，不写提升百分比。完成付费实验后，再从原始 JSONL 重算每成功任务成本、任务配对结果和失败归因。

## 系统工程主线

可以讲清三个不容易靠 SDK 封装解决的窗口：

1. 完整模型结果已收到，但 graph checkpoint 尚未写入：从业务账本复用响应，避免重新采样。
2. 命令或补丁已生效，但数据库尚无结果：查同一 job/receipt 或核验 after-hash；不确定就暂停，不盲目重放。
3. 取消、租约迁移和活跃进程同时发生：新 epoch 不是旧进程已停止的证明，必须保留执行器的实际观察。

Independent review 还发现并推动修复了 partial usage 被当最终费用、提前 `os._exit(0)` 被当测试通过、验证阶段修改原测试、工件归属不一致和总运行时间预算未及时停止等问题。面试时可以拿复现测试与修复依据说明设计如何变化，避免只展示一张功能架构图。

## 可使用的英文描述

> Built a Python coding-agent workbench with native OpenAI/Anthropic streaming adapters, PostgreSQL-backed execution ledgers, LangGraph recovery, and rootless Docker tool execution.

> Implemented versioned context evidence and independent patch verification; added fault-injection tests for interrupted model streams, persisted-response recovery, partial execution outcomes, and cancellation.

这些描述只陈述已实现的系统。只有在对应实验真实完成后，才补充模型效果或成本数字。不要把固定脚本的端到端测试写成“自主解决真实用户任务”。

## 尚可验证的研究假设

- evidence guard 的扫描/重读成本是否被更少的版本错误抵消？
- portable compaction 是否在同预算下优于只保留近期历史？
- B3 的收益是否优于固定强模型，而非仅仅来自使用更贵模型？
- 保守 unknown-cost 占用是否过度停止任务，怎样用可审计的账单对账改善？

允许实验给出负结果。一个能说明何时没有收益、为什么失败的项目，比没有原始证据的提升数字更能说明工程判断。
