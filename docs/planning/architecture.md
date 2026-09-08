# Forge：模块契约、执行一致性与证据保真设计

依据：`AGENT_IMPLEMENTATION_PLAN.md` 与本目录 `requirements.md`。本文件由独立规划 Agent 编写；接口命名可按实现调整，但字段语义和不变量不应悄悄删减。

## 架构及信任边界

```text
CLI ─HTTP/SSE→ API ─transaction→ PostgreSQL queue / ledger / event log
                                   ↑
                                   │ claim + fenced writes
                            Worker + LangGraph
                         /          │           \
                  ContextBuilder  Provider     ToolPolicy
                    │             adapters         │
              Retrieval + Artifacts     Rootless Docker runner
                                                │
                                        per-run workspace

Evaluation controller ─public task projection→ API
Evaluation grader ─clean checkout + patch + private tests→ independent result
```

API 不执行模型调用和仓库命令。worker 保持模型凭据、数据库权限和容器控制能力；任务容器不拥有这些能力。grader 是独立进程和工作区，其私有材料不进入 Agent payload、检索根、容器挂载或上下文工件。Task container 不得挂载整个项目根目录以“方便测试”。

CLI 只是 API 客户端。API、worker、evaluation 使用相同的 domain 类型，调用各自 application 服务；domain 不依赖 FastAPI、数据库会话或厂商 SDK。

## 模块所有权与契约

| 模块 | 输入→输出 | 自己负责 | 不应该拥有 |
| --- | --- | --- | --- |
| `domain` | Pydantic / enum / Protocol | 序列化、状态、错误、公共字段 | 网络/文件/DB 副作用 |
| `application` | validated command→run/decision/artifact | 用例事务、owner、幂等、状态版本 | prompt 或工具执行实现 |
| `persistence` | domain 操作→账本事实 | PG 约束、lease、event seq、模型/工具事实 | 根据模型文本推断成功 |
| `runtime` | run/state/ledger→图状态迁移 | 执行图、预算/停止、恢复、工具顺序 | 自己越过 policy 访问宿主命令 |
| `providers` | ModelRequest→ModelEvent 流→ModelOutput | 原生协议、能力校验、usage/错误 | 工作区、数据库事务或路由策略 |
| `context` | TaskState + transcript refs→ContextBundle | 预算、选择、摘要、证据有效性 | 修改代码、隐藏验收标签 |
| `retrieval` | query/path/snapshot→EvidenceRef[] | rg、按行读取、符号索引及 hash 失效 | 将无来源文本标成当前事实 |
| `tools` | validated call + policy→ToolResult | schema、allow/ask/deny、相对路径规则 | provider 选择、最终成功判定 |
| `execution` | authorized operation + epoch→job/receipt | workspace、Docker、锁、进程、核对 | 自动重放不确定命令 |
| `artifacts` | bytes + metadata→ArtifactRef | 原子写、hash、owner、大小限制 | 将任意外部路径直接返回客户端 |
| `telemetry` | attempt/usage/timing→ledger + spans | 费用占用/对账、脱敏、关联 | 把 unknown 计成零 |
| `evaluation` | public task + private spec + patch→Trial | 干净验证、指标、报告 | 信任模型 finish 或 Agent 自测作为最终判断 |

## 公共类型的最低语义

以 UUID/ULID 字符串标识逻辑对象；运行中的路径统一是 POSIX 相对路径。所有需要 hash 的 payload 使用规范 JSON 编码并记录算法版本，不能使用 `repr(dict)` 或 Python 的进程随机 `hash()`。

```python
class ArtifactRef(BaseModel):
    id: str
    kind: str
    sha256: str
    byte_size: int
    content_type: str

class EvidenceRef(BaseModel):
    id: str
    kind: Literal["file", "diff", "tool_output", "validation", "constraint"]
    source_id: str                 # path / call_id / validation_id / message_id
    workspace_revision: int
    content_sha256: str
    artifact_ref: ArtifactRef
    start_line: int | None = None
    end_line: int | None = None
    status: Literal["current", "historical", "stale", "missing"]

class ContextBundle(BaseModel):
    messages: list[CanonicalMessage]
    tools: list[ToolDefinition]
    evidence: list[EvidenceRef]
    token_budget: ContextBudget
    summary_version: int | None
    summary_ref: ArtifactRef | None
    native_state_ref: ArtifactRef | None
    context_hash: str

class ToolResult(BaseModel):
    call_id: str
    status: Literal["ok", "error", "denied", "timeout", "cancelled"]
    summary: str
    artifact_refs: list[ArtifactRef]
    workspace_revision: int
    exit_code: int | None = None
    truncated: bool = False
```

这些示例不是完整实现类型；所引用的 CanonicalMessage、ContextBudget、ToolDefinition 必须在代码中有明确 schema。若实现选择 artifact ID 字符串代替内嵌 ArtifactRef，应保持可解析的同等信息。

`Run.status` 与 `stage` 分开；`verification_status` 只能是 verified/regression_only/unverified。`completed` 只有经过预先固定目标验证的 run 才能使用；一般回归通过但目标未验证的任务以明确受限/失败结果结束并保留工件。

`ModelTurn` 唯一逻辑键包含 run、iteration、attempt；request/model/usage 与 raw/canonical/native artifacts 保存。`ToolExecution` 唯一逻辑键关联确认的 turn、ordinal 和 call_id。不能仅以 `args_hash` 去重所有工具：模型可能在不同轮次有意执行相同命令。

## ProviderAdapter

```python
class ProviderAdapter(Protocol):
    def capabilities(self, model_id: str) -> ModelCapabilities: ...
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
    async def compact(self, request: CompactRequest) -> CompactResult: ...
```

ModelRequest 包含 run_id、turn_id、model_id、canonical_messages、native_state_ref、tools、sampling_config、output_limit、deadline。ModelEvent 为 TextDelta / ToolCallDelta / ToolCallReady / UsageUpdate / TurnCompleted / ProviderError 判别联合。

单个 turn 的 reducer 必须累积工具参数，不将 ToolCallDelta 直接交给执行器。每个完整调用在 schema 校验通过之后被纳入 durable ModelOutput。出现流错误则保存 incomplete attempt 和费用状态；未确认输出不触发副作用。

provider 原生 state 包含 provider/model/版本标签。只有同 provider 的支持配置能消费它。跨 provider 迁移必须不存在 pending calls，保留公共 task/constraints/accepted decisions/tool results，重新校验当前 evidence。错误类型和 retry policy 分开：标准化错误不意味着所有错误都能重试。

## Runtime 与恢复协议

LangGraph 节点依赖账本引用，尽量不复制大量日志/代码。Graph checkpoint 是控制位置；业务账本与 job receipt 是外部操作的事实来源。

```text
initialize_workspace -> baseline_validate -> build_context -> model_step
model_step -> validate_tools -> permission_gate -> execute_tools -> ingest_results
model_step -> verify_candidate -> finalize | repair_feedback
ingest_results -> stop_check -> build_context | terminal
permission_gate -> waiting_approval -> requeue -> revalidate -> execute_tools
any safe boundary -> cancellation / budget / stalled / reconciliation
```

审批 interrupt 节点无副作用。用户批准不直接执行工具，而是写 decision 并重新排队；恢复后检查 tool args hash、workspace revision、policy 版本与当前 owner/lease。UserMessage 只在安全工具批次边界应用，追加记录并设置 applied_iteration。

每次工具操作：

1. 确认当前有效 epoch 与 workspace 写锁；检查没有失联活跃 job。
2. 验证工具参数与 policy，审批适用时验证其绑定。
3. 事务插入 planned ToolExecution，固定逻辑 job ID；提交后才启动执行。
4. executor 以 job ID/epoch 启动或查询原 job，持久化输出与 receipt。
5. 从 receipt 对账写入 succeeded/failed；记录事件；graph 吸收 ToolResult。

单机 executor 也要保持 epoch 持久化与每-run 串行写锁。仅 API/数据库拒绝旧 epoch 不能阻止已运行的旧进程继续写文件。领取新 lease 后，runner 先查询旧 job；确认终止或完成后才开放写锁。

未知结果处理：patch 检查预期前后 hash；命令查原 container/job/receipt；证据不充分则 needs_reconciliation。不得承诺任意命令 exactly-once。恢复一个已确认模型结果不调用模型；回放 UI 仅读 event/artifact，不重新执行。

## 工具与工作区契约

`read_file/list_files/search_code` 返回同 revision 的快照事实；并发上限默认 4。`apply_patch/run_command` 均持独占锁。读取完成后 revision 已变化时，结果可以保存为 historical，不能伪装为当前文件。

apply_patch 先解析全部受影响路径、校验 protected paths 和全部 expected_file_hashes，再准备变更。失败要保持整体可解释：可靠的原子多文件提交需要 staging/回滚策略及 receipt；若仅做到有序逐文件写，必须能在崩溃时识别部分状态并暂停，不能宣传原子 patch。

`run_command` 只传 argv 到容器；不使用 host shell。实际边界来自容器挂载、非 root、禁网、资源限制、policy 和安全路径访问。输出完整保存至 artifact，模型收到有限摘要和引用。timeout/cancel 要停止进程组并确认容器状态，不能只取消等待 stdout 的协程。

base commit 初始化须验证是允许 repo 的实际 commit。Agent 使用独立 checkout，不直接在用户原仓库运行。新增文件也进入最终 diff。可信 RepoProfile 决定预构建依赖镜像与验证命令；Agent 不能在普通任务中修改该 profile 或 grader 入口。

## 差异化设计：带版本的上下文证据

项目的可实验贡献是一个 Evidence Ledger：将上下文中的文件事实、工具输出与测试结论绑定到实际来源和版本，并在编辑、压缩、跨 provider 迁移后显式核验有效性。这是本项目的工程假设，不能在未经研究或测量时声称学术首创。

普通长对话常把早期文件内容与最新修改混在一起。本方案让 context selection 根据“事实是否仍成立”做处理，并保留需要重新确认的信息，而不是只按时间丢消息。

### 数据流

1. read/search 返回内容时，Evidence Ledger 记录路径、行区间、文件 hash、revision、tool call ID 和完整 artifact。
2. apply_patch 或任意 command 完成后扫描受支持工作区的文件变化。文件内容 hash 是有效性的权威，revision 只是快速提示；命令可能改变未知文件，不能只相信 patch 的 changed_paths。
3. 构建下一轮 context 时按 path/hash 复核文件证据。相同 hash 的证据可以复用；变化文件的旧内容标 stale，不能作为 current code 放回 prompt。
4. stale 证据仍保留在历史 artifact 中。若它是一个摘要决策的来源，则把关联 claim 放进 unresolved_items 或安排重新读取，而不是直接删除整个任务历史。
5. 测试结果关联执行时 workspace revision、相关 patch/snapshot hash、profile hash。后续修改使“当前测试通过”的声明失效，但不抹去历史成功记录。
6. portable summary 中的每条文件判断有 evidence ID，关键用户约束有 message ID。压缩结果经过 schema 与约束覆盖校验；无来源的新断言不能自动成为已确认事实。

### 保真摘要的接受条件

- 固定字段为 task、constraints、decisions、changed_files、test_state、unresolved_items、artifact_refs。
- 用户任务/约束从追加消息的可信公共记录机械保留；LLM 可以简述解释，不能替换唯一原文。
- 每个 changed_files 条目保留 path、最新 hash 与来源；失效的决策标为需复核。
- 成对 tool call/result 不拆散；pending call 必须完整保留，不能为了满足预算丢半条协议。
- 摘要未通过结构、来源、关键约束或预算校验时拒绝替换原状态，并采用确定性的裁剪/回读策略。
- 原始日志和代码总能通过 artifact/read_file 取回；摘要从不替代真实验证。

### 预算与选择

预算分项覆盖 system、tools、task_constraints、diff_tests、retrieval、recent_pairs、summary、output_reserve。每个分项都有 token count 与 exact/estimate 标记。超预算时先消除重叠代码、失效事实和重复日志，然后选择片段、压缩旧历史。任务约束与未决协议不够容纳时显式失败或切换到足够容量模型，不能静默截断。

`context_hash` 覆盖实际发出的规范化消息、工具 schemas、evidence IDs/hashes、summary version、native-state reference。artifact 中保存 selection reason，例如 `current_failure`、`definition`、`recent_tool_pair`、`constraint`；这让失败分析可以定位到底没加载什么。

### 可证伪实验

基础 B0–B3 保持原方案。新增诊断开关 `evidence_guard`，在 B2 相同模型、相同预算、相同长任务子集上比较开/关；不会新增一个未经批准的大系统。

任务包括：读文件后修改再继续推理、多文件重命名让旧行号过期、长测试输出压缩后再修改、用户中途追加约束、跨 provider 迁移后继续。它们必须有独立目标测试，不能只考是否输出固定短语。

指标包括：旧文件事实作为 current 内容进入 prompt 的次数、hash 冲突写入尝试、约束遗失次数、无必要重读量、context token 消耗、独立修复成功率、每成功任务成本、时延。stale 当前事实泄漏可以机械审计；“推理是否依赖旧事实”若无法严格判定则只报告人工错误分析，不伪造精确指标。

假设：在受上下文压缩影响的长任务上，evidence guard 减少版本错误且收益足以覆盖复核成本。可能负结果：hash 扫描/重读增加时延，任务过短无收益，严格拒绝摘要导致超预算。这些都应报告。

## 独立评测契约

EvalTask 的 public projection 只包含 repo/commit/task/profile 的公开部分。grader_spec_ref、hidden tests、reference patch、最终标签不进入 Agent 进程可访问的数据。评测调度器可以知道 trial 标签，但 runtime/router/context 的接口不能传入它们。

每 trial 从干净状态运行，grader 再从同一 base commit 新建 checkout，应用最终 patch，检查保护路径、固定目标测试与原有回归测试。Agent 自建测试可以作为附加证据；唯一验收来自独立规则。需要防止 patch 修改 conftest/sitecustomize/验证配置绕过 hidden checks，profile 显式定义允许变更路径和受保护验证入口。

任务集 30 项（10 dev/20 heldout）按缺陷家族/仓库分组。重复 3 次是同一个 task 的重复观测。报告 B0/B1/B2/B3 每 trial 原始值与按任务配对比较；B3 包含固定强模型对照。模型不可用/限流/沙箱损坏属于 infrastructure failure，不混为普通错误补丁。

## 工程证据与独立审阅

五份 ADR 至少回答：单 Agent 基线为何足够；context 选择与预算如何取舍；opaque 续接为何不能跨厂商；未知命令副作用如何暂停核对；独立 grader 为什么必须使用另一工作区/权限边界。Evidence Ledger 增加第六份 ADR，并链接正负实验结果。

独立审阅按需求矩阵检查，不只按代码风格。优先审阅：隐藏测试泄漏、host 执行降级、伪 verified、账本/checkpoint 不一致、old epoch 仍能写、重复计费漏记、上下文调用配对破坏、失效 hash 被当作当前事实。

评审发现必须包含可复现触发条件、实际路径/代码位置、用户可见影响、修复建议和验证结果。最终 README 准确区分 implemented/tested/live measured/optional。项目的可信度来自这些事实和取舍，不来自批量生成的功能列表或虚构工作经历。
