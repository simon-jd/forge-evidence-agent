# Forge：AI Engineer 实现需求与验收追踪

规划依据：仓库根目录 `AGENT_IMPLEMENTATION_PLAN.md`，完整覆盖其第 1–17 节。规划日期：2026-09-07。本文定义需要实现的行为和证明方式；“已规划”不等于“已实现”，FakeProvider 结果不等于真实模型效果。

这是独立规划 Agent 的交付。实际实现和独立审阅需要分别提交证据。项目是个人工程项目；只使用实际实现、真实运行和失败分析说明贡献，不将其包装成未发生的公司经历。

## 目标及范围

输入：允许来源的代码仓库、固定 `base_commit`、自然语言修复任务、可信 `RepoProfile`、冻结运行配置。输出：可应用补丁、独立验证结果、可回放轨迹、完整模型使用量与费用口径。

核心范围为单用户、Linux 单机、Python 仓库、多任务独立工作区。API 与 worker 分进程。用户可以提交、观察、补充要求、审批、取消与恢复任务。最终交付包含 OpenAI Responses 与 Anthropic Messages 两个原生适配器、LangGraph 持久图、PostgreSQL 业务账本、rootless Docker 执行、上下文与路由策略、独立评测、运行文档和证据。

不要求第一版实现：公开多租户、训练平台、插件市场、任意恶意仓库的强安全保证、复杂多 Agent 团队。原方案第 16 节扩展明确列为独立 feature flag，不能用其替代核心验收。

## 证据规则

每个需求记录以下状态之一：`planned`、`implemented_unverified`、`verified`、`external_dependency`、`optional_deferred`。只有检查真实源码与对应执行结果之后才能标记 `verified`。

验收清单以预期证据描述，初始状态全部 `planned`，扩展为 `optional_deferred`。实施时补充命令、输出工件、环境、commit 和日期。不得根据一个广泛命名的测试文件或绿色 CI 推断覆盖了整行需求。

建议将证据落在 `docs/validation/` 与 `results/`：机器可读结果保留原始值，人工文档只归纳观察结果；模型与价格配置保留冻结版本。含凭据或敏感内容的原始响应使用本地受控 ArtifactStore，Git 只提交脱敏 fixture 与可共享的评测结果。

## 需求矩阵

| ID / 原节 | 需要实现的行为 | 主要归属 | 通过条件与证据 |
| --- | --- | --- | --- |
| R01 / 1,3 | 输入 repo + commit + task，产生 patch、验证、轨迹、usage；独立任务空间；API 断开后继续 | application / runtime / execution | 三个公开 Python fixture 的端到端报告；两个任务同时执行且互不修改；断开 watch 后任务仍完成 |
| R02 / 2,4 | Python、uv lock、FastAPI/Pydantic、Typer/HTTPX、LangGraph、SQLAlchemy/Alembic/PostgreSQL、Docker、Ruff/mypy/pytest；保持模块单向依赖 | packaging / all | 锁定依赖；新环境安装、迁移与启动；静态检查通过；禁止 API 直接调用 provider、provider 读取 workspace、grader 复用 agent 判断 |
| R03 / 5 | Project、RepoProfile、Run、UserMessage、AgentState、ModelTurn、ToolExecution、Approval、RunEvent、Artifact、EvalTask、EvalTrial 全部具有原方案要求的字段与约束 | domain / persistence | 可逆/可前进迁移策略；数据库唯一约束真实测试；配置完整序列化；金额与未知 usage 不丢失 |
| R04 / 5,10,14 | run 冻结模型能力、模型 ID、提示词/schema hash、预算、context 参数、依赖版本、镜像 digest 与价格版本 | config / telemetry | 已创建 run 不受之后配置文件变化影响；导出的 manifest 可以解释每一费用和上下文决策；别名明确标记不可完全复现 |
| R05 / 6 | 原生 OpenAI Responses / Anthropic Messages 流适配；标准事件可归并；保留 opaque 原生状态 | providers | 分片 JSON、多个工具、文本混排、usage、错误结束、流中断协议测试；两个 provider 分别真实 smoke 并保存脱敏证据 |
| R06 / 6 | 参数完整校验+落账后才执行；call/result ID 配对；不支持能力显式报错；标准错误与有限抖动退避 | providers / runtime | 未完成 JSON 不触发工具；认证/无效请求不重试；429/临时错误有上限；SDK/application 只有一个重试负责层；每次尝试独立账单 |
| R07 / 6,9,10 | 跨 provider 仅在完整工具轮次结束后迁移；用公共事实重建上下文；原生续接仅回原 provider | providers / context | 未决调用时拒绝切换；OpenAI opaque 项不会传入 Anthropic；迁移后合法工具 transcript、用户约束和最新文件引用仍完整 |
| R08 / 7 | 初始化→构建上下文→模型→校验→权限→工具→结果→停止判断；finish/最终文本都进入验证 | runtime | 有工具/纯文本结束/验证失败修复/超预算/取消分支均执行；不能用模型声明或任意退出码 0 直接判成功 |
| R09 / 7,12 | 基线验证；`verified` / `regression_only` / `unverified`；补充消息安全边界应用且幂等；检测停滞 | runtime / evaluation | 目标测试缺失只能 regression_only；基线失败有工件；补充消息不会覆盖原任务、改变权限或 grader；无变化循环按阈值停止 |
| R10 / 8 | 七工具：list_files、read_file、search_code、apply_patch、run_command、get_diff、finish | tools / retrieval | 行号/hash/截断信息；ripgrep 精确来源；patch 前置 hash 全部校验；diff 含新增文件；完整命令输出入工件；finish 仅候选 |
| R11 / 8 | 工具 schema 与 allow/ask/deny；审批绑定参数 hash + workspace_revision；只读并发、写与命令独占 | tools / persistence | deny 不执行；过期审批无效；并发 patch/command 不竞写；审批重试幂等；仓库文本不能改变 policy |
| R12 / 8 | 固定 commit 独立 checkout；显式 snapshot 才纳入未提交修改；rootless Docker 的非 root、只读 rootfs、tmpfs、CPU/mem/PID 限额、禁网、无宿主 key/socket | execution / docker | 真实 Docker 检查挂载、网络、资源与 user；任务写不到授权目录外；原仓库不被修改；可信预构建依赖镜像使用 digest |
| R13 / 8,15 | 防路径穿越和符号链接越界；限制输出；超时与取消终止进程组；不把 argv 当完整安全边界 | execution / tools | `../`、绝对路径、symlink 读写、路径替换、长输出、子进程持有 stdout、超时后残留进程、grader 路径写入等实际边界测试 |
| R14 / 9 | ContextBundle 含消息/工具/引用/预算/provenance/native state；固定任务约束→diff/测试→按需代码→近期成对调用→旧摘要 | context | 导出逐轮 snapshot；预算包含 schema/system/output reserve；estimate/exact 标签；不能裁掉未决调用的一半；完整日志可回读 |
| R15 / 9 | 文件/关键词检索 + tree-sitter 符号索引；按 snapshot/path/hash/parser version 缓存；修改失效 | retrieval | 名称/定义查找有效；变更文件只失效对应索引；不返回旧 hash 为当前事实；大仓库限制扫描大小与输出 |
| R16 / 9 | portable_summary 固定 schema + native compaction；70% 可调触发；先去重/过期/长日志；摘要文件事实带 hash | context / providers | task、constraints、decisions、changed_files、test_state、unresolved_items、artifact_refs 通过 schema；缺约束拒绝采用；原生能力缺失显式选择 portable 或 UnsupportedCapability |
| R17 / 10 | 固定模型基线与有限规则路由；无效参数/重复失败/能力不足触发；单独事件；不可访问隐藏标签 | runtime / context | 升级次数与余额限制；路由理由可回放；强固定模型对照；价格/能力来自冻结配置而非硬编码 latest |
| R18 / 10 | token/cost/calls/runtime 上限；调用前预留、结束后实际对账；失败/未知 usage 不计零；Decimal 费用 | telemetry / runtime | 边界预算拒绝调用；重试计费；中断请求保留 estimated/unknown 占用；取消不能声称已发出请求免费；零成功的 cost/success 为未定义 |
| R19 / 11 | PostgreSQL SKIP LOCKED 领取、lease epoch fencing、心跳；事件 seq 单调；checkpointer 与账本事实分离 | persistence / runtime | 两 worker 同时领取不会双持有有效租约；旧 epoch 不能写入或启动新 job；LangGraph PostgreSQL checkpoint 独立 namespace；PostgreSQL 上测试，SQLite 替身不足以验收 |
| R20 / 11 | 完整模型响应先记账；planned tool→稳定 job ID→receipt→DB→graph；恢复先核对 | runtime / execution | 完整响应在 checkpoint 前崩溃不重调已确认模型；patch 应用后崩溃可按前后 hash 识别；命令结果未知进入 needs_reconciliation，绝不盲目重放 |
| R21 / 11 | 旧 lease 不意味着旧命令已停；批准后入队恢复；取消直到确认活动 job 停止才终态 | runtime / execution | 工具运行中 worker 崩溃仍能查 job；续租失败后拒绝新 job；审批时重启；取消与完成竞态；保留已有补丁、usage、日志 |
| R22 / 12 | 原方案十二个 REST endpoint；单用户 token、loopback 默认、owner 检查、allowed repo roots | api | API 合同测试覆盖创建项目/任务、状态、消息、取消、恢复、审批、事件、工件、评测提交/查询；无权限不能访问他人 run/artifact；终态不误恢复 |
| R23 / 12 | POST run/message 幂等；持久 RunEvent SSE，Last-Event-ID 续接、seq 去重、文本分块 | api / persistence | 重复相同 key 返回同对象，冲突 body 拒绝；重连无遗漏；持久后发送；慢客户端不导致无限内存；客户端断开不终止 worker |
| R24 / 12 | CLI run/watch/approve/cancel/resume/diff/eval；公共 API 客户端 | cli | 新环境对运行中的 API 完成整套命令；CLI 无数据库/worker 内部依赖；退出码和拒绝原因可用于脚本 |
| R25 / 13 | 独立 evaluator 在干净 checkout 应用 patch；隐藏测试、参考 patch、标签不暴露；public projection | evaluation / execution | 实际检查 Agent mount 和提交给 Agent 的 JSON；patch 可应用+目标通过+回归通过+保护路径未改；删除测试/skip/重写 grader 不能假成功 |
| R26 / 13 | 冻结 30 个诊断任务：10 dev / 20 heldout，覆盖定位、多文件、回归、长输出，按仓库/缺陷家族分组 | evals / evaluation | 数据集 manifest、task ID、commit、split、family、镜像、grader 规范齐全；任务有实际缺陷与独立验证；split 冻结记录，不能临时复制同一任务凑数 |
| R27 / 13,14 | B0 基线、B1 选择、B2 压缩、B3 路由；同预算配对比较；建议每 task/config 3 次 | evaluation | 真实模型逐 trial JSONL/CSV 与 HTML；配对的 task-level 分析、样本数与限制；强固定模型作为 B3 对照；真实 API 与 fake/fault 结果分开 |
| R28 / 13 | success_within_budget、总成本/成功数、latency median/p95、阶段耗时、tokens、calls、compaction、invalid args、错误分类 | evaluation / telemetry | metrics 从原始 trial 重算一致；失败花费计入；infra 与 agent 失败分别计数；重复尝试不当独立 task；若报告 CI，按 task 分组 bootstrap |
| R29 / 14 | 结构化日志、OpenTelemetry run trace + 阶段 span、run/iteration/call/provider/prompt/context/revision 关联 | telemetry | 实际本地 exporter trace 能串联模型→工具→验证；密钥脱敏；artifact owner 与保留策略；不记录或索要隐藏推理 |
| R30 / 15 | 单元、contract、integration、fault、端到端及真实 smoke 分层 | tests / CI | 普通 CI 不调用付费模型；独立 live 命令需显式模型和费用上限；R05/R13/R19–21 故障与边界逐项覆盖 |
| R31 / 17 | 代码、lock、容器、迁移、测试、原始 eval、报告、ADR、README、演示 | docs / all | 新环境按 README 跑 smoke；演示真实修复/进程故障恢复/预算停止/固定任务比较；ADR 覆盖单 agent、context、原生续接、未知副作用、评测分离 |
| R32 / 用户 | 独立 Agent 规划、开发、审阅；创新有实现和实验；子目录项目 | docs / workflow | 本 planning 文档；开发实现；独立审阅按 commit 和路径指出问题并复验；所有项目文件位于 forge；公开说明个人项目，无虚构经历或提升数字 |
| R33 / 16 | MCP、Skills、只读子任务、原生异步工具、多语言 profile、本地推理 endpoint、Web | feature flags | optional_deferred；若启用须独立合同/取消/权限/效果验收，不能默认声称支持 |

## 必须显式验证的故障窗口

这些条目不可被“一般恢复测试通过”替代。每个注入点应有持久状态、恢复动作和不允许行为的断言。

| 故障点 | 恢复时读取的事实 | 禁止行为 |
| --- | --- | --- |
| 模型流中途断开 | 未完成 ModelTurn、计费占用、没有 ToolCallReady | 执行部分 JSON；把未知费用记为 0 |
| 已收到并入账的模型输出，checkpoint 前崩溃 | 完整 ModelTurn canonical/native artifact | 为已确认逻辑轮次重新采样 |
| 工具开始后 worker 退出 | planned/running ledger、job ID、Docker label、receipt | 仅凭新 lease 就启动重复命令 |
| patch 已应用，结果入库前退出 | patch 前后 hash、工作区 revision、receipt | 再应用一次；覆盖未知用户/命令变更 |
| 命令完成，receipt 或 DB 未同步 | 容器退出状态、持久输出与 receipt | 找不到结果就重放命令 |
| 等待审批时重启 | Approval args_hash、workspace_revision、checkpoint | 重新执行审批前有副作用代码；自动批准新参数 |
| 续租失败产生旧 worker | 当前 epoch、job ownership、旧进程存在性 | 旧 worker 覆写新状态；新 worker 与旧 job 同时写 |
| cancel 与工具完成同时发生 | cancel_requested、job terminal receipt、状态版本 | 命令仍活跃就报告 cancelled；丢失已产生工件 |

## 依赖顺序与开发拆分

阶段是技术依赖，不是时间安排。

1. A：domain、配置快照、ArtifactStore、FakeProvider、仓库 profile、初始迁移；先做完整 fake 任务验证接口。
2. B：workspace、七工具、Docker runner、独立 candidate verifier、基本 runtime 与 CLI/API；三个不同 fixture 都生成可应用补丁。
3. C：两原生 provider、流合同、usage、错误归一与 live smoke；成本账本从此覆盖每次模型尝试。
4. D：PostgreSQL scheduler、LangGraph PostgreSQL checkpoint、UserMessage、审批、取消、SSE 与 owner/idempotency 约束。
5. E：执行账本、稳定 job、lease fencing、receipt/reconciliation 与所有故障窗口。恢复路径必须使用 B 的真实 runner。
6. F：context provenance/预算/关键词与符号检索/摘要/路由。可以与 D/E 局部开发，但最后要接入同一 runtime 和持久账本。
7. G：独立 eval 从 B 开始；冻结任务集后才比较 B0–B3。任务构建、测试本身正确性和 public projection 不依赖模型好坏。
8. H：独立审阅、修复、完整验收证据、README 新环境演示、ADR、可分享仓库。

允许独立开发 Agent 负责 `providers` 或 `context/retrieval` 等低耦合模块。修改公共 `domain` 类型时先同步契约。审阅 Agent 不以自己刚写的测试作为唯一依据，应检查真实的 API→worker→runner→grader 链路和外部依赖证据。

## 外部依赖与不能伪装完成的事项

| 依赖 | 所需能力 | 无法获得时仍可做的工作 | 必须保留的缺口 |
| --- | --- | --- | --- |
| Python/uv 与包索引 | 安装受支持的固定 Python 小版本与 lock 依赖 | 写类型、配置、测试和 docs | 未安装的 SDK/checkpointer 不能算集成通过 |
| rootless Docker daemon | task sandbox、独立 grader、故障注入 | 单元/合同、显式非沙箱开发后端 | 不允许将 host subprocess 验证称为 Docker 隔离验证 |
| PostgreSQL | SKIP LOCKED、约束、迁移、checkpoint | 仓储 fake 合同 | SQLite/内存 fake 不足以证明租约/事务正确 |
| OpenAI / Anthropic 凭据与额度 | 真实 provider smoke 和质量 eval | 模拟流与错误合同、报告生成器 | 两真实 provider smoke 及真实 B0–B3 原始结果仍未完成 |
| 明确可用的模型与价格 | 冻结 model ID、能力、价格、币种、生效时间 | example 配置与校验器 | 不硬填过时 ID/价格；未知不能算免费 |
| ripgrep / tree-sitter grammars | 代码/符号检索 | 文件读取与 parser 合同 | tree-sitter 功能必须有真实 parser fixture |
| GitHub 授权（用户允许新建） | 可选远程仓库创建与 push | 本地完整项目和 commit | GitHub 不是核心 runtime 依赖；不可上传 secrets/private grader 原始敏感材料 |

仓库创建已由用户授权。远程名、可见性、发布内容按实际环境处理；GitHub 的存在本身不证明项目完成。真正完成要求 R01–R32 的当前证据，R33 明确可选。

## 交付完成审计

- 从干净环境安装锁定依赖、迁移数据库、启动 compose，并运行 README smoke。
- 查看真实任务 patch、基线与目标/回归验证工件；验证失败不能显示已验证成功。
- 在真实 PostgreSQL/Docker 组合中演示 restart、cancel、stale lease、unknown effect 等恢复分支。
- 两 provider 实际调用经过同一工具与验证链路，协议测试与 live 结果分开保存。
- 30-task manifest 与 split 已冻结；保留任务与 grader 在运行时没有暴露给 Agent。
- B0–B3 报告有原始试验明细、真实成本口径、失败/负结果和明确的不确定性；不存在占位提升数字。
- 独立审阅发现的问题有对应修复和复验记录；没有以“测试未发现”代替需求完成证明。
