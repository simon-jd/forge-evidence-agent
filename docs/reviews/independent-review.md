# Forge 独立实现审阅

初次审阅：2026-09-07；补充审阅：2026-09-08。审阅 Agent：`independent_review`。该 Agent 未参与产品实现；仅新增 `tests/review/test_independent_findings.py` 和本报告。修复由开发 Agent 完成。初次五条回归由审阅 Agent 亲自复验；补充集成验证由主开发 Agent 执行，证据归属在下文分别说明。

初次审阅时项目尚无可解析的 Git HEAD，文末保留当时关键源码 hash。2026-09-08 的补充源码审阅绑定提交 `04ce87db3bfb29c43b557723281c4e2fc375d35d`；后续变更不自动继承本报告的验收结论。

## 结论与检查范围

本次找到并实际复现了 **3 个 P1、2 个 P2**。修复后五条独立回归在专用 rootless Docker 上全部通过。未确认 P0。这个结论仅覆盖下列复现条件，并不表示所有需求、真实模型质量或任意恶意代码隔离均已验收。

阅读范围：原始 `AGENT_IMPLEMENTATION_PLAN.md`、独立 planning 文档，以及 API、应用服务、SQLAlchemy 业务账本、LangGraph runtime、worker、两个原生 provider、工具/工作区/容器执行、上下文/预算、评测调度/独立 grader/报告模块。重点检查了副作用恢复、epoch fencing、取消、费用完整性、测试篡改、工件所有权和公共评测投影。

独立执行的测试：原有 unit/contract 测试，以及本次新增的五条回归。API 工件回归使用 SQLite 验证业务对象与授权映射；它不是 PostgreSQL 锁或调度正确性的证明。此审阅 Agent 未另行运行完整 PostgreSQL 故障矩阵，相关结论需引用项目实际 PostgreSQL integration 验收记录，不能由这里的绿色结果推断。

## 已复现、已修复、已复验的问题

| ID | 级别 | 原触发点与用户影响 | 修复后独立结果 |
| --- | --- | --- | --- |
| IR-01 | P1 | Anthropic 流中途 usage 被结算为最终精确费用，释放未知输出预算 | 通过；断流仍保留未知/估计占用 |
| IR-02 | P1 | 被测源码提前 `os._exit(0)`，没有执行测试却被 CandidateVerifier 标记 verified | 通过；没有完整测试运行证据不会 verified |
| IR-03 | P1 | 被测源码 import 时重写 protected public tests，独立 grader 仍给 verified | 通过；grader 工作区只读，修改不能发生 |
| IR-04 | P2 | verifier 子工作区工件列在主 run 下，但下载时身份不一致返回 409 | 通过；真实 verifier 输出的所有列出工件均可由 owner 下载 |
| IR-05 | P2 | 总运行时只在节点边界检查，长节点越过预算后仍继续 | 通过；worker monitor 在节点运行期间停止超时任务 |

### IR-01：未完成 Anthropic 流释放费用占用

原路径：`providers/anthropic.py` 的 `message_start`/`message_delta` usage → `providers/base.py:BaseProvider.stream` 的 failed/cancelled attempt → `context/budget.py:BudgetManager.reconcile`。

复现使用安装的原生 Anthropic SDK 与 `httpx2.MockTransport`，不调用真实模型：发送 `message_start`，usage 为 input=10/output=1；随后返回大量文本，然后在最终 usage/`message_stop` 前 EOF。原实现把 input/output 都非空视为精确，reservation 状态变成 `settled`。测试配置下，原本预留 `$0.001010` 只计 `$0.000011`，释放 `$0.000999`，足以允许一个本不应被批准的新请求。这里的价格仅为可计算的 fixture，不是厂商实际报价。

初次修复在非完成流的 attempt 中明确标记估计 usage；复验断言 reservation 不为 settled，后续请求不能使用被错误释放的额度。后续 `usage_final` 契约进一步区分中途计数与已经确认的最终 usage：没有最终计费证据的失败/取消仍保守占用；收到最终 usage、但工具参数校验失败的响应可以按已知费用结算，见补充审阅。

测试：`test_interrupted_anthropic_usage_does_not_release_unknown_output_budget`。

### IR-02：测试未运行却通过候选验收

原路径：`runtime/verification.py:CandidateVerifier.verify`。原判定只要求 regression/target 命令返回 0 且工作区内容不变。

复现：基线 `maths.add` 仍是错误减法，protected `test_public.py` 完全不变；只在允许修改的 `maths.py` 开头添加 `import os; os._exit(0)`。两个 `python -m unittest ...` 命令在加载测试时就退出。原实际 Docker 结果是 `regression=0; target=0; validation_workspace_unchanged=True`，返回 verified，实际执行了零个验收用例。

修复增加受信 unittest/pytest completion 探针、collection/execution IDs 与 counts、skip/failure 检查；runtime 比较基线测试选择。任意普通 argv 的退出码不自动取得 verified 资格。验证工作区也设为只读。

复验确保提前退出缺少完成记录时不能 verified。此测试不证明同一 Python 解释器能抵抗任意主动篡改 runner 内存的代码；completion 记录不是密码学可信执行证明。

测试：`test_zero_exit_before_acceptance_executes_cannot_be_verified`。

### IR-03：评测时修改受保护回归测试

原路径：`evals/grader.py:DockerGrader.grade` 与 `GRADER_RUNNER`。原实现仅在 patch preflight 保护 `tests/**`；整个 grader 工作区执行时仍可写，且先 import target 模块再 discover public regression。

复现：patch 只修改 allowlist 内的 `maths.py`。候选模块 import 时把 `tests/test_public.py` 替换为一个空 pass 用例；`add` 固定返回 5，仍违反原负数回归。独立目标检查正数案例通过，随后 grader 收集到替换后的空回归。原实际 Docker 返回 verified，target_tests_run=1、regression_tests_run=1。这不需要 monkeypatch Python 测试解释器。

修复使用只读 grader workspace，并检查执行前后 sealed snapshot；隐藏目标、回归、runner 不能通过执行过程改变。复验该候选不能 verified，正常修复仍由另外的工件测试真实通过。

测试：`test_grader_rejects_protected_test_mutation_during_import`。

### IR-04：子工作区工件的归属映射不一致

原路径：`runtime/verification.py` 产生 helper-run 的 applied_patch artifact；`runtime/engine.py:Runtime._save` 将其 ArtifactRow 绑定主 run；`api/app.py:get_artifact` 检查 metadata.run_id 时拒绝。

原复现中，owner 能在 `/v1/runs/{id}/artifacts` 看见该条目，但 GET `/v1/artifacts/{ref}` 返回 409。这个问题发生在实际包含补丁的验证流程，不是跨用户请求。

修复在 verifier 将输出复制为主 run 拥有的工件，业务账本拒绝不一致归属；API 的完整性与 owner 检查保持有效。独立回归更新为真实 Docker CandidateVerifier → Runtime._save → API 列表/逐个下载，不要求保留旧的错误子 run ID。

测试：`test_verifier_artifact_list_entry_is_downloadable_for_run_owner`。

### IR-05：长节点越过总运行时上限

原路径：`worker.py:Worker._observe`。原 monitor 只检查取消、epoch 和心跳；`Runtime.step` 的总时长检查发生在进入节点之前。

独立单元复现将 run 的 `max_runtime_seconds` 设为 1，预算 started_at 设为 10 秒前，然后让节点继续执行。原 monitor 等待节点自然结束，没有停止它。在真实流程中，长模型流或 profile 允许的长命令因此能越过总运行时上限。

修复在 monitor 中检查持久预算状态，到期后取消图、等待活动执行停止，再记录 budget_exhausted；无法确认时继续使用 reconciliation。独立回归验证活动节点被取消。该用例只证明 monitor 分支；实际 Docker 子进程清理与 PostgreSQL 终态落账应由对应 integration tests 证明。

测试：`test_worker_observer_enforces_total_runtime_while_node_is_running`。

## 2026-09-08 补充：原生参数错误反馈与有限升级

此前开放项现已完成**独立源码审阅与主开发集成验证**，不再保留为未解决的实现缺口。本次未新增产品代码或扩大审阅范围。

源码检查确认以下路径：

- 原生 adapter 对已确认响应中的未声明工具、JSON/schema 错误使用 `invalid_tool_arguments`，与 HTTP 无效请求的 `invalid_request` 分开。反馈只包含已声明工具名和固定校验说明，不回显任意参数或未声明工具名称。
- `Runtime._model_error` 为该错误增加总计数和连续计数、清除 native state，追加“该响应未执行任何工具”的校验反馈，然后回到 context。它没有把无效调用写成待执行工具，也不构造悬空 call/result 续接。
- router 使用连续错误计数及冻结配置的阈值、升级次数、费用/调用预算决定是否升级；成功接受完整 turn 后清零连续参数错误计数。该路径是下一逻辑轮次的模型修复，不是对 HTTP 400 的自动传输重试。
- `UsageUpdate.final` / attempt `usage_final` 保留计费终态语义。OpenAI completed 响应和 Anthropic 完成协议确认后的最终 usage 可以在随后工具 schema 校验失败时结算；中途 usage 与未确认流仍保留估计/未知占用。

主开发 Agent 在真实 PostgreSQL 与专用 rootless Docker 上执行了 `tests/integration tests/review`，现有 [full-integration-junit.xml](../validation/full-integration-junit.xml) 记录 **33 tests、0 failures、0 errors、0 skipped，40.545 秒**。审阅 Agent 已读取该文件，确认其中 `test_native_invalid_arguments_receive_bounded_feedback_and_route` 通过，耗时 2.452 秒。

该指定测试使用原生 OpenAI SDK 和 MockTransport 的固定协议响应，经实际 runtime/业务账本与 rootless 验证链路完成：两次无效参数只调用弱模型，之后升级一次；请求顺序为 weak、weak、strong、strong；两个无效响应均不执行工具；四次模型尝试全部记账；最终有效补丁通过独立验收。测试还断言参数错误计数为 2、升级事件为 1、只有后两轮存在工具执行。

**证据边界：**上述 33 项由主开发 Agent 执行，不是审阅 Agent 亲跑。审阅 Agent 最初的指定测试尝试因子 Agent 旧 sandbox 无法连接本地 PostgreSQL而失败，异步合同测试也未正常完成；这些环境失败没有计入产品结果。补充审阅确认了脱敏反馈的源码实现，但没有额外声称测试覆盖了所有可能的敏感参数组合。该指定测试直接驱动 runtime stages，不单独证明 LangGraph checkpoint 重启；后者有另行的图恢复测试。整个路径使用合成 provider 响应，**没有调用真实模型 API，也没有验证实际模型的修复质量或升级收益**。

## 其他源码检查与尚未确认的范围

- 已指出两个集成缺口：tree-sitter SymbolIndex 原先未接入 Agent 工具链；`.env` 的厂商 API key 原先未被 SDK 默认读取。开发者随后接入 `search_code(mode="symbol")`，并使用显式 SecretStr 配置注入 provider。这里确认了修复源码的存在；不能据此声称真实账号调用已成功。
- 原生工具参数校验失败的反馈/路由缺口已关闭，依据是上述独立源码审阅与主开发 PG/rootless 集成验证。真实账号调用、实际模型连续错误行为与质量收益仍需 live 实验，不能由合成协议测试推断。
- 代码中不存在发现后仍允许缺失 command receipt 直接重放的路径：执行器保留 job identity，缺原容器会进入 reconciliation；完整 patch after-state 才能复用。这个判断来自源码及既有测试检查，不代替完整 PostgreSQL/进程故障现场复验。
- Rootless 复跑证明的是本报告三条实际 Docker 路径。CPU throttling、OOM、PID exhaustion、旧 lease 多 worker 交错等需引用各自专门测试，不能由 rootless socket 可用推断。
- 30-task qualification 明确区分 fixture/reference validation 与模型质量。本审阅未执行两个真实 provider 的付费 smoke、B0–B3/strong-only 质量实验或 evidence_guard 开关消融；没有可报告的质量提升、成本下降或真实模型成功率。
- 公共投影与只挂任务工作区的设计阻止了正常 Agent 工具直接读取私有 grader 材料。当前单机受控威胁模型不提供任意 hostile Python、host-root、容器逃逸或同 UID 控制进程间的强隔离证明。

## 实际执行记录

初始回归在明确启用的 rootful development daemon 上重现 IR-01、IR-02、IR-03，结果为失败断言；IR-04、IR-05 在本地业务/monitor 回归中重现。它们是缺陷证据，不能算当时验收通过。

修复后：

```bash
.venv/bin/python -m pytest tests/unit tests/contract -q
# 143 passed in 3.21s

FORGE_REVIEW_DOCKER=1 FORGE_REVIEW_ROOTFUL=1 \
  .venv/bin/python -m pytest tests/review/test_independent_findings.py -q
# 5 passed in 3.11s

FORGE_REVIEW_DOCKER=1 \
FORGE_REVIEW_DOCKER_HOST=unix:///tmp/forge-rootless-1000/docker.sock \
  .venv/bin/python -m pytest tests/review/test_independent_findings.py -q
# 5 passed in 3.24s; 未启用 rootful fallback

.venv/bin/ruff check tests/review/test_independent_findings.py
# passed
```

测试镜像实际 ID：`sha256:88ea842ecc6d79db7d22c0cf911ea41b5ebec7c6846298eea1ae7a77dc2c7e15`。没有调用付费模型，没有把真实 API 密钥写入测试或本报告。命令在工具执行沙箱外经授权运行；最初受限执行环境会卡在 asyncio 通知/清理，同命令在授权环境正常完成。这是验证环境限制，不是产品测试通过的替代。

关键修复源码快照 SHA-256（记录时尚无 Git HEAD）：

```text
1c44d9d41a360ef8e1c2d8bc0bd3a94987c29653f4a7ae9e6917bce992d2be34 providers/base.py
a73637139f5f8622ac83db7fdf412de27b0733f7833d3870f0161620d1b60c83 runtime/verification.py
180384ca7c810efecfbcd4ae002e83d9de2ecd4d15eef4e4ba62b9d40bca63c9 runtime/test_runner.py
efc2bb294a49a07937f821f1cecf54cbb8f815c8d22c40c51bfee546fa485cd8 evals/grader.py
50aae395e6b3882aa3261e7c41ab39b6dfb11a7be8bf6779ad9dcc8ead6cb0da worker.py
a3478a3a3bd3af740d981794211dad96e8af1b006f48e1da7f0fdd006d59a693 runtime/engine.py
```
