# DeepSeek 固定评测出站数据独立审计

日期：2026-09-11。范围为 `configs/campaigns/deepseek-10usd.json` 冻结的 12 个任务及 `examples/demo-repository`，只读审计；没有请求模型、运行 campaign 或修改冻结配置。

**结论：本次实际指定的源码输入均为 Forge 自建的小型诊断样例。已检查范围内没有公司项目代码、真实客户数据、凭据或用户个人文档。** 这是对下述固定数据和当前实现的范围结论，不是对任意仓库内容作保证。

## 检查到的输入

- 独立读取 12 个任务的全部 Python 源文件、公开测试、任务说明和约束；其内容是事件订阅、幂等请求、内存补丁、并发资源释放、工具上下文、行号、语法树、流式 usage、配置覆盖、UTF-8 字节队列、预留金额及日志筛选的人工构造缺陷。
- 唯一较大的 `bounded-log-tail/sample.log` 共 1,600 行、46,414 bytes。逐行归一化数字后，全部内容只有 `ERROR dependency import failed` 与 `INFO executor heartbeat` 两种合成模板，没有真实运行日志内容。
- 独立读取 demo 的 `README.md`、`replay.py`、`test_cursor.py`；内容为合成事件游标分页示例及公开验收测试。demo 任务与补充指令同样只描述该例子的预期行为。
- 合计 **31 个文件、56,771 bytes**。12 个任务所有文件的 SHA-256 与 `evals/manifest.json` 匹配；实际文件集合与 manifest 完全一致。demo 仅有上述 3 个文件；全部已审计目录均无符号链接或额外文件。

按路径排序，对每个文件 `{path, bytes, sha256}` 使用 `json.dumps(items, sort_keys=True, separators=(',', ':'))` 编码后，独立得到 inventory SHA-256：`490735e985065aec1230ece847d53c652f1854fe88e54ae085b325f0089d9d0b`。

## 实际出站与隔离边界

| 数据 | 当前代码路径与结论 |
|---|---|
| 模型请求正文 | `ContextBuilder` 组合固定系统提示、上述任务说明、约束、当前 run 的工具结果和代码片段；DeepSeek adapter 将这些 canonical messages 和工具 schema 转为 Responses 请求。后续发送内容还包括模型自身生成的候选修改、此隔离样例的测试输出与摘要。 |
| 源码快照 | `Dataset.materialize` 只复制对应 fixture 并核对文件清单、hash、固定 commit；`WorkspaceManager.prepare` 只对该 commit 执行 `git archive`。不会将 Forge 总仓库、工作区其他项目或未提交 `.env` 复制进去。 |
| 私有 grader 与参考答案 | `EvalTask.public_projection` 不包含 `target_source`、`reference_patch`、`author_note`。私有 spec 在 agent callback 返回后才交给独立 `DockerGrader`，写入另一个 grader workspace；评分结果不再回流到本轮 agent。正常 agent 的 `target_test_argv` 被设为空。 |
| 用户目录及本机凭据 | 文件工具经 `SafeRoot` 限制在各自 worktree，拒绝绝对路径、`..`、符号链接；artifact 读取还校验同一 run。Docker 只挂载该 worktree，network 为 none，不挂载家目录、Docker socket、私有 spec 或 `.env`。 |
| 环境变量 | 宿主 API/worker 可读取本地 DeepSeek key 完成认证；执行容器环境只设置固定 HOME/Python/沙箱变量，不继承宿主 API keys。key 用于 `https://api.deepseek.com` 的 Authorization 认证，不进入任务文本。adapter 显式去除环境中 OpenAI 自定义 headers、organization、project 等可能的额外凭据。 |

可见的运行标识、相对文件名、内容 hash、工具参数和生成测试输出属于这次诊断工作所需数据。无需发送原公司的代码、本机其他仓库或完整主项目源代码，即可完成该固定 campaign。

该审计可作为重新评估**同一个、同一数据范围、同一 $10 总授权**执行请求的低风险证据；不替代自动审批结果，也不授权扩大数据范围或绕过已发生的审批拒绝。
