本方案用于实现 Forge：一个面向代码修复任务的多模型 Agent 平台，服务于 AI Engineer / Applied AI 求职项目。

调研日期：2026-09-07。本文是可交给开发 Agent 的实现规格，不包含工期安排。文中的接口、默认值和验收条件属于本项目设计；模型能力与 API 行为应以链接的官方文档和实际账号能力为准。

1. 明确产品目标与第一版边界。

    用户提供一个代码仓库、固定的起始 commit 和一个修复任务。系统在隔离工作区内定位问题、编辑代码、运行检查，交付 patch、验证结果、执行轨迹和费用统计。用户能查看进展、批准受控操作、取消任务，以及在进程重启后继续任务。

    第一版支持单用户、单机 Linux、Python 仓库的完整修复与评测流程；Agent 的读写和命令工具保持语言无关。后续通过 RepoProfile 增加 Go、Rust、C++ 仓库的构建与验证能力。支持多个独立任务，每个任务拥有独立工作区。

    产品入口采用 CLI，管理接口采用 HTTP API。Web 界面是 API 的后续客户端，不承担核心执行逻辑。OpenCode 的客户端与服务端分离方式可供参考。[OpenCode Server](https://opencode.ai/docs/server/)

    核心交付必须包括：

    - 能实际生成修复补丁的单 Agent 执行循环。
    - OpenAI、Anthropic 两个原生模型适配器。
    - 按需代码检索、上下文预算和会话压缩。
    - 工具权限、容器执行、持久化与中断恢复。
    - 使用独立测试判定修复效果的评测系统。
    - 可回放的运行记录，以及质量、时延、成本实验报告。

    第一版不实现通用聊天门户、模型训练平台、公开多租户服务、计费系统、插件市场和复杂多 Agent 团队。每个扩展须有独立验收条件，不能阻塞上述核心闭环。

2. 使用一套明确的主技术栈。

    | 层 | 选型 | 实现责任 |
    | --- | --- | --- |
    | 主语言 | Python，固定一个受依赖支持的小版本 | Agent、API、worker、评测共用类型与工具代码 |
    | 依赖管理 | uv + lockfile | 锁定 SDK、图运行时、解析器和测试依赖 |
    | API 与类型 | FastAPI + Pydantic v2 | 输入校验、OpenAPI、任务控制、SSE |
    | CLI | Typer + Rich + HTTPX | 提交任务、事件展示、审批、取消、结果下载 |
    | 执行编排 | LangGraph StateGraph | 节点路由、checkpoint、暂停与恢复 |
    | 模型调用 | openai、anthropic 原生 Python SDK | 保留厂商专有的工具、usage 和续接语义 |
    | 业务持久化 | PostgreSQL + SQLAlchemy + Alembic | 项目、任务、执行账本、事件、审批、评测 |
    | 图持久化 | LangGraph PostgreSQL checkpointer | 图状态恢复；使用独立 schema/table namespace |
    | 任务调度 | PostgreSQL 任务表 + 租约 | API 入队、worker 领取、心跳和恢复 |
    | 工作区执行 | rootless Docker + Docker SDK | 每个 run 独立工作区和容器 |
    | 代码检索 | ripgrep、文件分段读取；之后接 tree-sitter | 关键词、符号、定义所在文件的定位 |
    | 工件 | ArtifactStore 接口，先落本机持久化卷 | 补丁、完整命令输出、上下文快照、测试报告 |
    | 可观测性 | 结构化日志 + OpenTelemetry | 跨模型调用、工具和验证阶段的 trace |
    | 验证 | pytest、Ruff、mypy、固定测试容器 | 协议、故障恢复、端到端行为与 Agent 效果 |

    Go、Rust、C++ 不进入第一版服务实现。只有测量得到瓶颈后，才分别考虑用 Go 拆出执行 worker、用 Rust 实现高吞吐代码索引。C++ 仓库支持通过构建 profile 实现；模型推理使用既有服务端，不自行实现推理引擎。

    LangGraph 仅管理执行流程；检索策略、压缩策略、模型选择、工具实现和评测代码均属于本项目。它的 checkpointer 与跨会话 store 是不同能力，第一版主要使用前者。[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

3. 按以下运行拓扑组织代码。

    ```mermaid
    flowchart TD
        C[CLI / 后续 Web 客户端] --> A[FastAPI：提交、审批、事件]
        A --> D[(PostgreSQL：任务、账本、checkpoint)]
        D <--> W[Python Worker + LangGraph]
        W --> X[上下文构建与模型适配器]
        X --> M[OpenAI / Anthropic]
        W --> T[工具校验与执行器]
        T --> S[每任务独立容器与工作区]
        W --> F[ArtifactStore]
        E[独立评测进程] --> A
        E --> V[干净环境中的验证容器]
        V --> F
    ```

    API 和 worker 是同一代码库中的两个进程。API 提交任务后立即返回 run_id，worker 独立消费任务；客户端断开连接不影响执行。第一版由 Docker Compose 启动 API、worker 和 PostgreSQL。

    worker 是可信控制进程，负责访问模型 API 和控制容器；模型能调用的命令运行在任务容器里。容器内不放模型 API key、数据库凭据和 Docker socket。

    独立评测进程拥有隐藏测试与参考修复的访问权限。Agent 执行进程不能读取它们。

4. 创建以下目录，并保持模块边界。

    ```text
    src/forge/
      api/                 # routes、auth、SSE、异常映射
      cli/                 # API 客户端与展示
      domain/              # 公共 Pydantic 类型、状态、错误类型
      runtime/             # graph、nodes、stop_policy、resume
      providers/           # base、openai、anthropic、capabilities
      context/             # builder、budget、compaction、provenance
      retrieval/           # files、ripgrep、symbols、repo_profile
      tools/               # registry、schemas、policy、handlers
      execution/           # sandbox、jobs、workspace、reconciliation
      persistence/         # models、repositories、leases、event_log
      artifacts/           # ArtifactStore、local backend
      telemetry/           # spans、usage、pricing、redaction
      evaluation/          # datasets、runner、grader、metrics、reports
    configs/
      models.example.yaml
      policies.example.yaml
      repo_profiles/
      experiments/
    prompts/               # 版本化 system、compact、repair 提示词
    tests/
      unit/
      contract/
      integration/
      fault_injection/
      fixtures/
    evals/
      tasks/               # 提供给 Agent 的任务元数据
      grader_private/      # 仅 evaluator 可访问的隐藏验证材料
    migrations/
    docker/
    docs/decisions/         # 架构取舍及实验结论
    ```

    API 只能调用 application/domain 接口，不能直接调用模型。模型适配器不能访问工作区。工具不能决定路由和模型选择。grader 不复用 Agent 对“成功”的判断。

5. 先定义领域对象与数据库约束。

    | 对象 / 表 | 必需字段与约束 |
    | --- | --- |
    | Project | id、repo_source、default_profile、owner_id |
    | RepoProfile | language、image_digest、trusted_setup、validation_argv、allowed_patch_paths；由用户或管理员配置 |
    | Run | id、project_id、base_commit、task、status、verification_status、config_snapshot、config_hash、lease_epoch、lease_expires_at、cancel_requested |
    | UserMessage | id、run_id、seq、text、applied_iteration；补充指令以追加记录保存，不能覆写原任务 |
    | AgentState | run_id、stage、iteration、transcript_refs、task_summary、active_model、native_state_ref、pending_call_ids、workspace_revision、budget_state |
    | ModelTurn | run_id、iteration、attempt、provider、requested_model、reported_model、raw_response_ref、canonical_output_ref、usage、finish_reason |
    | ToolExecution | id、run_id、turn_id、ordinal、args_hash、workspace_revision、lease_epoch、status、sandbox_job_id、result_ref；唯一约束覆盖一个已确认的逻辑调用 |
    | Approval | id、run_id、tool_execution_id、args_hash、workspace_revision、decision、decided_by；审批绑定具体操作与工作区版本 |
    | RunEvent | run_id、seq、type、payload、created_at；唯一键为 run_id + seq |
    | Artifact | id、run_id、kind、relative_key、sha256、byte_size、content_type |
    | EvalTask | id、repo_source、base_commit、task_text、profile_id、split、grader_spec_ref |
    | EvalTrial | task_id、experiment_id、repeat_id、run_id、outcome、metrics、failure_category |

    Run.status 使用：queued、running、waiting_approval、completed、failed、cancelled、budget_exhausted、needs_reconciliation。stage 表示执行阶段，不能与最终状态混用。

    config_snapshot 必须包含模型 ID、能力配置、提示词 hash、工具 schema hash、检索与压缩参数、预算、验证 profile、依赖版本以及镜像 digest。模型别名无法解析成固定版本时明确记录，报告不得声称完全确定性复现。

    SQLAlchemy 业务表与 LangGraph checkpoint 没有天然跨库事务保证。业务账本记录模型响应和工具效果，图状态保存账本 ID。恢复时从账本核对事实，不能仅凭图节点重跑来推断外部操作状态。

    RunEvent 是客户端事件与审计记录；checkpoint 是图恢复记录。界面回放读取事件和工件，不重新执行模型或工具。

6. 实现模型适配层，并保留厂商语义。

    以以下契约为基础；实现时补全所有被引用的 Pydantic 类型和 Python Protocol：

    ```python
    class ProviderAdapter(Protocol):
        def capabilities(self, model_id: str) -> ModelCapabilities: ...
        def stream(
            self, request: ModelRequest
        ) -> AsyncIterator[ModelEvent]: ...
        async def compact(
            self, request: CompactRequest
        ) -> CompactResult: ...

    # ModelEvent 的判别联合：
    # TextDelta | ToolCallDelta | ToolCallReady | UsageUpdate
    # | TurnCompleted | ProviderError
    ```

    ModelRequest 必须包含 run_id、turn_id、model_id、canonical_messages、native_state_ref、tools、sampling_config、output_limit、deadline。CanonicalMessage 以 role + content_parts 表达文本、文件引用、工具调用、工具结果；厂商原生不透明项目通过 native_state_ref 保存，不能强行转成文字。ModelTurn 的标准输出包含 visible_text、complete_tool_calls、finish_reason、usage、native_state_ref，流事件最终必须能归并成这一对象。

    ModelCapabilities 至少描述：tool_calling、structured_output、parallel_tool_calls、native_compaction、native_async_tools、tool_search、context_window、max_output_tokens，以及经过验证的组合约束。

    实现要求：

    - OpenAI 使用 Responses API；Anthropic 使用 Messages API。
    - 通用 transcript 记录用户输入、可见回复、工具调用和工具结果；另存厂商原生 payload / continuation state。
    - 工具参数流必须组装完成、通过 schema 校验并持久化，之后才允许执行。
    - 保持调用与结果 ID 一一对应；跨模型调用不得遗留未解决的工具调用。
    - 厂商不支持的能力返回 UnsupportedCapability，或者选择显式标记的本地策略；不得静默丢参数。
    - usage 保留 input、output、cached 等厂商提供的分项。缺失值标记 unknown，不能计为零。
    - 不同厂商的错误归一为 rate_limited、transient_provider_error、invalid_request、context_exceeded、authentication_error、stream_interrupted。
    - 对限流和暂时服务异常做有上限、带抖动的退避；无效请求和认证错误不自动重试。
    - 每次模型尝试独立记账，SDK 重试与应用重试只保留一个负责层，避免重试倍增。

    Anthropic 的 client tools 由应用执行并回传 tool_result；保持 tool_use_id 关联。[Anthropic Tool Use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)

    跨 provider 切换只能发生在完整模型轮次、已处理完工具结果的边界。用任务摘要、文件引用和完整的公共工具记录重建新上下文。OpenAI 的压缩结果含不透明状态；不能将其当成通用摘要传给另一厂商。[OpenAI Compaction](https://developers.openai.com/api/docs/guides/compaction)

    原生异步工具调用是扩展能力：厂商支持时可启用，但任务生命周期、工具执行与取消仍由本平台负责。普通 asyncio 并发与原生异步工具调用是两套机制，应分开配置、分开测试。[OpenAI Async Tool Calling](https://developers.openai.com/api/docs/guides/async-tool-calling)

7. 实现单 Agent 的执行图。

    ```text
    initialize_workspace
      -> build_context
      -> model_step
        -> 有完整工具调用：validate_tools -> permission_gate
           -> 已允许：execute_tools -> ingest_results -> stop_check
           -> 待审批：interrupt -> 恢复后重新校验 -> execute_tools
        -> 提议完成：verify_candidate
           -> 验证成功：finalize
           -> 验证失败且有预算：反馈失败信息 -> build_context
           -> 无验证条件：生成未验证工件，以失败/受限结果结束
        -> 临时模型错误：受控重试
        -> 预算耗尽、取消、不可恢复错误：对应结束状态
    stop_check -> build_context / 停止
    ```

    模型依据工具结果决定下一步动作；graph 管理校验、审批、执行和停止。任务描述、用户约束与验证目标始终保留。不要强制每轮增加一个独立 planner 模型调用。

    build_context 前读取尚未应用的 UserMessage，把补充要求合入可见任务约束并记录 applied_iteration。第一版在当前工具批次完成后的边界接受调整；用户要立即停止则使用 cancel。调整使待执行操作失效时丢弃旧候选，重新生成；补充消息不改变平台权限或隐藏验证规则。

    先实现 read_file、list_files、search_code、apply_patch、run_command、get_diff、finish 七类工具。finish 只提出完成候选，由验证节点决定结果。

    模型返回普通最终文本时，也必须进入完成判定；不能绕过验证。调用 run_command 得到退出码 0 不能单独成为任务成功条件。

    初始化时运行可信的基线验证，保存已失败测试和可复现目标。verification_status 分为 verified、regression_only、unverified：只有满足预先固定的任务验收条件才标记 verified；原有测试全部通过但缺少目标缺陷的验证证据时，只能报告 regression_only，不能声称已证明修复。对应 Run 不进入已验证完成状态。

    初始默认参数可配置：max_model_turns=40、max_tool_calls=100、max_repair_rounds=2、max_concurrent_read_tools=4；运行时还需独立的 token、费用、命令超时和总运行时上限。这些是防止失控的工程初始值，须通过实验调整。

    检测重复工具参数、相同错误和无工作区变化的循环。达到阈值后允许一次明确的策略调整；仍无进展则停止，记录 stalled。

    LangGraph 中 interrupt 恢复会重新进入节点，审批前的代码可能再次执行。因此审批节点应保持无外部副作用，并把实际工具执行放入单独节点。[LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

8. 实现工具契约、权限与工作区执行。

    ```python
    class ToolResult(BaseModel):
        call_id: str
        status: Literal['ok', 'error', 'denied', 'timeout', 'cancelled']
        summary: str
        artifact_refs: list[str]
        workspace_revision: int
        exit_code: int | None = None
        truncated: bool = False
    ```

    | 工具 | 输入 | 执行规则 |
    | --- | --- | --- |
    | list_files | relative_path、glob、limit | 限定仓库根目录，忽略大目录与二进制文件 |
    | read_file | relative_path、start_line、end_line | 返回行号、文件 hash、截断标记 |
    | search_code | query、path_glob、limit | ripgrep，输出路径、行号、片段和文件 hash |
    | apply_patch | patch、expected_file_hashes | 先验证所有前置 hash；一次有序写入；输出变更文件与新 revision |
    | run_command | argv、cwd、timeout_seconds | 容器内执行，保存 stdout/stderr、退出码和资源统计 |
    | get_diff | 可选文件列表 | 相对 base_commit 生成补丁，包含新增文件 |
    | finish | summary、claimed_changes | 输出完成候选，触发独立验证 |

    read_file/list_files/search_code 可以在一致的工作区版本上并发。apply_patch 与所有命令默认独占该 run 的写锁；命令即使看似只读，也可能产生文件或启动进程。

    第一版 run_command 使用 argv 执行，默认不暴露隐式宿主 shell。确需 shell 表达式时使用独立工具契约，并仍然在容器内执行；argv 本身不是安全边界。

    每个任务从固定 commit 创建独立 checkout/快照，只挂载自己的工作区。原始仓库默认只读；用户未提交的修改必须通过显式 snapshot 选项纳入，并记录快照 hash。

    执行配置包括：非 root 用户、只读容器根文件系统、独立临时目录、CPU/内存/PID 限额、默认禁网、无特权模式、无宿主凭据与 socket。依赖安装放在可信镜像构建步骤，任务容器使用预构建镜像。

    读取、写入都要校验路径规范化、符号链接和工作区边界；修改工具采用目录句柄/安全文件访问方式降低检查与使用之间的竞态。容器挂载边界提供第二层约束。

    权限策略返回 allow / ask / deny，绑定工具类型、目标路径、网络和具体参数。仓库文字和工具输出按数据处理，不能修改权限策略；审批不会解除容器资源与挂载边界。

    第一版的威胁模型是受控单用户环境中的不可信模型输出。普通 Docker 容器不构成任意恶意代码的绝对隔离保证。公开运行不可信仓库前，应单独实现更强的 VM / microVM / gVisor 执行后端并重新验收。[Claude Code Sandboxing](https://code.claude.com/docs/en/sandboxing)

9. 实现按需检索与上下文构建，把它作为 Applied AI 的主要实验模块。

    ContextBuilder 输入任务状态与 transcript，输出 ContextBundle：messages、工具集合、文件引用、预算分项、压缩信息、provenance、native_state_ref。

    上下文按以下顺序分配：

    - 固定保留：用户任务、约束、验证目标与平台工具规则。
    - 当前工作：最新 diff、相关失败测试摘要、尚未解决的问题。
    - 按需检索：文件片段、符号定义、调用位置和必要测试。
    - 近期完整交互：保留成对的工具调用和结果。
    - 旧历史：结构化摘要与可回读的工件引用。

    首先实现文件名/关键词检索和按行读取；再实现 tree-sitter 符号索引。索引键包含 repo snapshot、路径、文件 hash、parser 版本；编辑文件后使该文件索引失效。

    不默认建设向量数据库。只有关键词与符号检索的失败分析显示语义定位缺口时，再加入 embedding 检索作为独立可切换实验。检索返回文件位置和来源，不能只给无出处摘要。按需加载与轻量文件引用可参考 Anthropic 的上下文工程实践。[Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

    预算计算必须覆盖系统指令、工具 schema、消息、文件和预留输出。使用可用的厂商 token 计数接口或经校准的保守估计，并记录 estimate/exact 标签。输入达到可用预算的 70% 触发候选裁剪/压缩；该阈值属于可调参数。

    裁剪优先处理重复文件片段、过时工具输出与超长日志。完整内容保存成工件，模型可再次读取。不能丢弃一个仍等待结果的工具调用，或构造不合法的调用/结果序列。

    CompactionSummary 使用固定 schema：task、constraints、decisions、changed_files、test_state、unresolved_items、artifact_refs。文件事实带 hash，重新读取前核验版本。摘要是有损状态，不能取代真实代码和验证结果。

    提供两条压缩策略：portable_summary 用于可迁移任务摘要；provider_native 用于厂商支持的原生续接。原生不透明状态必须保留在原厂商协议里，跨厂商迁移用公共记录重新构建。

    为每次构建记录 token 分布、加载片段数量、检索耗时和摘要版本，支持在同一任务集上做有无检索、有无压缩的消融实验。

10. 实现有限、可解释的模型选择与费用控制。

    ModelRegistry 用配置声明 provider、model_id、能力、上下文容量、价格版本和用途标签，不把“最新模型”硬编码进业务逻辑。真实模型 ID 从当前账号可用模型中选择并冻结在实验配置里。

    先实现固定模型 baseline。随后增加规则路由：以较低成本模型开始，在重复修复失败、工具参数连续无效、上下文能力不足等可观测触发条件下升级。升级原因进入事件日志，升级次数有上限。

    路由决策只使用运行前或运行中可见信号，不访问隐藏测试、参考补丁或最终标签。不使用模型自报置信度作为唯一依据。

    API 暂时失败时先按错误类型重试。需要换 provider 时，必须先关闭当前不完整轮次、核对已执行工具并满足迁移边界。流中断不代表模型请求没有计费，也不代表工具没有执行。

    BudgetManager 在调用前预留估算额度，调用后使用实际 usage 对账；不明费用保留保守占用并标记 estimated/unknown。费用上限属于应用可控制的后续请求限制，不能保证取消已经发出的请求会撤销计费。

    金额使用 Decimal 或整数最小计费单位，价格来源与生效版本随实验冻结。模型调用后的价格变化不回写既有实验的费用计算。

    同时设置 max_input_tokens、max_output_tokens、max_calls、max_cost 和 max_runtime。大模型升级只在剩余额度能覆盖该请求时进行。

11. 实现任务恢复，并明确一致性边界。

    API 在事务中创建 queued Run。worker 使用 FOR UPDATE SKIP LOCKED 领取任务，增加 lease_epoch，并周期续租。所有状态更新携带 epoch，拒绝过期 worker 写回。

    每个完整模型输出先写 ModelTurn 账本，再让 graph 引用它。graph 在相同逻辑轮次恢复时优先读取已完成响应，避免为了恢复已经确认的输出而再次调用模型。

    执行有副作用工具之前，先写 planned ToolExecution；执行器使用持久化 job ID 和容器标签关联任务。执行结果先形成可核验 receipt/工件，再提交 succeeded/failed 状态，graph 随后吸收结果。

    必须处理“命令已运行但数据库未写回”的不确定窗口：

    - apply_patch：核验操作前后文件 hash 与补丁状态；已完整应用则复用结果，部分状态不明则暂停核对。
    - run_command：查找原容器、job 状态和持久化 receipt；仍在执行则接管观察，已结束则读取结果。
    - 找不到可确认结果时，标记 needs_reconciliation，不自动重放可能有副作用的命令。

    领取租约不等于旧命令已经停止。新 worker 必须先确认旧 job 完成或终止，才能重新开放该工作区的写操作。执行器检查 epoch，失去租约时停止启动新 job；旧命令的终止与结果核对属于恢复协议的一部分。

    waiting_approval 不占用活动 worker。审批入库后重新排队，从 checkpoint 恢复，并重新检查参数 hash、权限与 workspace_revision。重复审批请求必须幂等；改变过的操作需要新的审批记录。

    取消操作设置 cancel_requested；worker 停止新模型调用，并终止受控容器进程组。只有确认活跃 job 停止后，才能把任务标记 cancelled。保留已生成的 patch、usage 和日志。

    第一版恢复范围限定为单机进程故障、网络中断和容器进程异常，依赖 PostgreSQL 与工作区持久化卷仍可用。完整机器或存储损坏恢复需要独立备份/快照方案。

    对外只承诺记录可回放和已确认步骤可恢复。不能宣称任意 shell 命令的 exactly-once 执行，也不能把模型重新采样称为确定性回放。

12. 实现 API、事件协议和 CLI。

    | 方法与路径 | 行为 |
    | --- | --- |
    | POST /v1/projects | 注册用户允许的仓库来源与可信 RepoProfile |
    | POST /v1/runs | 输入 project_id、base_commit、task、config_id；返回 202 和 run_id；支持 Idempotency-Key |
    | GET /v1/runs/{id} | 状态、当前阶段、预算、验证摘要 |
    | GET /v1/runs/{id}/events | SSE；使用 Last-Event-ID 恢复订阅 |
    | POST /v1/runs/{id}/messages | 追加补充指令；在下一安全执行边界应用；支持请求幂等 |
    | POST /v1/runs/{id}/cancel | 幂等取消请求 |
    | POST /v1/runs/{id}/resume | 仅恢复明确允许恢复的状态；校验状态版本；已完成任务需创建新 run |
    | POST /v1/approvals/{id}/decision | 绑定审批版本的 allow/deny 决策 |
    | GET /v1/runs/{id}/artifacts | 列出 patch、日志、报告 |
    | GET /v1/artifacts/{id} | 检查所有权后返回工件内容 |
    | POST /v1/evaluations | 提交配置化实验；使用受限的本地评测权限 |
    | GET /v1/evaluations/{id} | 结果与报告位置 |

    事件类型至少包括 run.created、stage.changed、model.started、text.delta、tool.planned、tool.finished、approval.required、context.compacted、model.switched、validation.finished、run.finished。

    对每个 run 单调分配 seq；持久化后再发送事件。文本增量可合并成有大小限制的块，避免逐 token 数据库写入。客户端按 seq 去重，断线后从已有游标继续。

    第一版默认绑定 loopback，使用配置的单用户访问令牌；仓库注册限制在允许路径/来源内。远程开放前补齐 TLS、身份与对象授权测试。数据库和工件都以 owner_id 关联，避免未来扩展时重做所有权边界。

    CLI 支持：forge run、forge watch、forge approve、forge cancel、forge resume、forge diff、forge eval。CLI 调用公共 API，不直接读 worker 状态或数据库。

13. 从第一条真实任务开始建设独立评测。

    EvalTask 固定仓库、commit、任务文本、镜像 digest 和验证规则。Agent 在正常工作区运行；结束后，evaluator 在干净 checkout 中应用生成的 patch，运行不受 Agent 控制的验证命令和测试。

    参考补丁、隐藏测试和标签放在 evaluator 专用目录/卷，不能挂载到任务容器。EvalTask 传给 Agent 的内容必须经 public projection 过滤，不能直接序列化完整数据库记录。

    修复成功要求：patch 可应用、目标失败测试通过、原有回归测试通过、没有修改被保护的 grader/验证配置、没有通过删除测试或跳过执行获得成功。Agent 新增的测试可以保留，但不能成为唯一成功依据。

    先建立 30 个任务的诊断集合：10 个开发任务、20 个保留任务，覆盖问题定位、多文件修改、回归修复与长输出处理。尽量按仓库/缺陷家族划分，冻结后不对保留集反复调参。该规模只能支持项目级实验结论。

    官方 SWE-bench 子集作为后续外部验证；使用其官方 harness，记录所选 task ID、版本和配置。自建集合与官方分数分开报告。[SWE-bench Evaluation](https://www.swebench.com/SWE-bench/guides/evaluation/)

    实验配置：

    | 实验 | 对照变量 | 要验证的假设 |
    | --- | --- | --- |
    | B0 固定模型基础循环 | 固定模型、基础工具、近期历史 | 建立实际质量和成本基线 |
    | B1 增加上下文选择 | 与 B0 同模型同预算 | 检索和日志裁剪能否减少输入，同时保持任务完成率 |
    | B2 增加压缩 | 在长任务子集与 B1 比较 | 摘要能否维持任务约束和跨轮次一致性 |
    | B3 规则模型路由 | 与固定较强模型对照 | 升级策略能否改善质量与成本的取舍 |

    每个配置/任务建议重复 3 次，每次从相同干净状态开始。报告每次独立尝试的成功率，不把“3 次里选最好一次”混作单次成功率。记录随机参数；模型服务不保证因此确定性。

    必需指标：

    - success_within_budget：固定预算内通过独立验证的比例。
    - total_cost / successful_tasks：包含失败任务花费的每成功任务成本；零成功时为未定义。
    - task_latency 的中位数与 p95；单独统计模型、检索、执行和验证耗时。
    - 输入/输出/cached tokens、工具调用次数、压缩次数、无效参数次数。
    - infrastructure_failures 与 Agent 解题失败分别统计。
    - 约束遗失、错误定位、错误补丁、回归失败、超预算、权限拒绝、循环停滞的错误分类。

    报告须提供样本数、逐任务结果和不确定性说明；跨配置比较按任务配对。重复试验不是新的独立任务，置信区间可使用按任务分组的 bootstrap。

    不预先承诺成功率提升或费用降低的数值。若优化无效，保留负结果与失败分析。代码任务的主判定来自可执行检查，LLM judge 只用于辅助归类或代码质量评语。[Anthropic Agent Evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

14. 实现可观测性与实验复现材料。

    每次 run 使用一个 trace，模型调用、检索、压缩、工具和验证使用子 span。关联 run_id、iteration、call_id、provider、model、prompt_hash、context_hash、workspace_revision。

    默认日志不记录 API key 和完整敏感文件；本地工件按所有权限制访问，配置明确的保留策略。调试记录以可见消息、工具参数、工具结果和状态迁移为核心，不要求厂商提供隐藏推理文本。

    模型 usage 与费用账本可从 trace 跳转查看；价格记录带版本与币种。不完整请求的费用显示估计/未知标记，不能静默丢弃。

    每个实验导出 JSONL/CSV 明细与 HTML 报告，至少展示成功率、每成功任务成本、时延分布和错误分类。README 中提供复现命令、数据集获取方式、固定配置和小规模 smoke eval。

15. 按依赖顺序实现，并逐项通过验收。

    | 执行顺序 | 实现内容 | 验收门槛 |
    | --- | --- | --- |
    | A | 领域类型、数据库迁移、FakeProvider、ArtifactStore、RepoProfile | 用固定响应能走通模型调用→工具→结果→验证；所有配置可序列化 |
    | B，依赖 A | 工具与容器、单模型循环、CLI 最小入口 | 三个公开 fixture 仓库可产生 patch；结果由独立验证器判定；任务容器不能写入授权挂载以外的宿主路径 |
    | C，依赖 B | 两个原生 provider 适配器、usage、错误归一 | 分片 JSON、多个工具、错误返回、中断流的协议测试通过；两个真实 provider 各跑 smoke case |
    | D，依赖 C | PostgreSQL 调度、checkpoint、审批、取消、SSE | API 断线不丢任务；事件可续接；重复提交不创建重复 run；审批绑定操作版本 |
    | E，依赖 D | 执行账本、租约 fencing、reconciliation | 指定故障点下无自动重复写入；未知副作用进入待核对状态；旧 worker 不能覆盖新状态 |
    | F，依赖 C/E | 上下文检索、预算、摘要、条件路由 | 能导出每轮上下文预算；摘要保留关键约束；切换 provider 不生成悬空调用 |
    | G，依赖 B，随 F 扩展 | 冻结任务集、独立 grader、实验报告 | 输出 B0–B3 的真实明细、失败分类与可复现配置；保留集从未暴露给 Agent |
    | H，依赖 G | 使用文档、演示、架构决策记录 | 新环境按 README 可运行 smoke case；可演示取消、恢复、补丁与评测报告 |

    一般单元/协议测试使用 FakeProvider 或脱敏固定记录；真实 API smoke 与质量 eval 使用显式命令和费用上限，不能使每次普通 CI 随机调用付费模型。

    故障注入至少覆盖：模型流中断；收到模型输出后 checkpoint 前崩溃；工具开始后 worker 崩溃；补丁已应用但结果未入库；命令完成但 receipt/数据库状态不同步；等待审批时重启；续租失败产生旧 worker；取消与工具完成竞态。

    工具边界测试覆盖：路径穿越、符号链接越界、超时与子进程终止、超长输出、workspace hash 冲突、仓库文本试图改变工具权限，以及试图改写 grader 的补丁。验证实际边界，不依赖模型是否“听话”。

16. 将扩展能力放在单独的 feature flag 后。

    - MCP：接入一个受控的文档检索 server，沿用相同工具注册、校验与审批。工具定义按需加载；MCP 作为工具连接协议，不替代 runtime、权限与沙箱。[MCP Architecture](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture)
    - Skills：先实现项目级只读说明文件与按需加载的任务说明，不允许技能文本直接提高权限。记录技能来源和版本。
    - 只读子任务：在单 Agent 基线成熟后，增加定位/审查子任务，每个子任务独立上下文、共享总预算，仅返回带文件引用的结果；主 Agent 保持唯一代码写入者。必须通过实验验证收益。
    - 原生异步工具：只对厂商支持且语义独立的工具启用；保持 call_id 与 job 生命周期关联，单独测试取消与延迟结果。
    - 多语言仓库：为 Go、Rust、C++ 各增加可信 RepoProfile、固定构建镜像和独立任务集，再报告对应支持范围。
    - 推理服务：有本地模型对照需求时增加独立 endpoint 适配器和能力测试；不把模型权重部署成功当作 Agent 效果提升。
    - Web 客户端：复用 REST/SSE，实现任务轨迹、审批、diff 和实验报告查看；避免引入第二套执行逻辑。

17. 用可核验的交付物说明项目贡献。

    必须交付运行代码、锁定依赖、容器配置、迁移、测试、原始评测结果、报告和架构决策记录。决策记录至少解释：为什么使用单 Agent 基线；如何取舍检索与上下文成本；跨厂商续接的限制；工具恢复如何处理未知副作用；为什么评测与执行环境分离。

    演示脚本包括一次实际修复、一次进程故障恢复、一次预算停止，以及一个固定任务集上的对照报告。报告明确哪些结果来自真实 API，哪些来自故障注入/FakeProvider。

    简历只能填写已实现和已测量的结果。例如：Built a multi-provider coding-agent platform with isolated tool execution, resumable workflows, and held-out repository-level evaluation; measured [actual result] across [actual task count] tasks under fixed budgets.

    将公司经历中自己实际负责的多模型接入部分与本项目独立完成的 runtime、context 和 eval 分别说明。本项目的主要个人贡献应能沿着“设计假设→实现→失败案例→对照实验→改进”完整解释。
