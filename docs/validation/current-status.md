# 实现与验证状态

更新日期：2026-09-13。项目范围是 AI Engineer 方案，位于独立的 `forge/` 子目录；没有把另一份 SDE 方案的可选组件混入主实现。以下结果来自本项目实际执行，不代表公司经历；离线工程验证与真实模型诊断结果分别记录。

## 遥测故障隔离补充（2026-09-13）

本轮按 [近期官方岗位与依赖研究](../research/market-2026-09-13.md) 修复具体控制路径问题：本地 JSONL 与 OTLP 各有独立有界 SDK 队列，显式配置 HTTP timeout，异步关闭并报告导出计数；阻塞、失败和队列满不再通过 exporter 或 SDK 队列警告同步阻塞 span 生产者。固定 span 字段、禁用自动异常载荷、窄范围 SDK 日志过滤减少敏感文本进入 trace 的路径。未知队列指标保留 null；SDK 指标须显式 export 环境变量。

实际验证：离线 **319 passed、3 skipped**；真实 PostgreSQL/rootless runtime、公共 CLI 和容器审阅 **22 passed、0 skipped**（包含此前跳过的三个审阅反例）。独立遥测审阅 5 项通过，包含在离线组内。Ruff 通过，mypy 58 个文件通过。各组重叠，不累加。50ms 本机慢 collector、6 对独立进程、每组 24 次同量 HTTP 的测量中，单次 p95 事件循环延迟的跨运行中位数从 56.231ms 降至 0.385ms；关闭排空另计，不能解释为模型或生产吞吐改善。见 [完整验证、原始样本与失败尝试](telemetry-20260913/README.md)。

本轮未升级依赖、未调用付费模型、未修改原始 DeepSeek 试验账本。关闭是 best effort，不能强制终止任意 Python exporter 线程；该项目仍为本地个人项目。下文 9 月 11 日及更早检查保留为历史记录。

## DeepSeek 与累计预算补充（2026-09-11）

已接入 DeepSeek Responses API、独立凭据、完整请求预留和峰值费率上界口径；实际 PostgreSQL/rootless Docker 集成使用模拟 HTTP 验证生产 factory 与 read → patch → finish → 独立验收。新一轮单元/协议 **202 passed**，完整集成/审阅 **42 passed**，均无跳过；合计 244 项不同测试。Ruff 通过，mypy 在 57 个文件中通过。详见 [DeepSeek 验证记录](deepseek.md)。下表保留 9 月 8 日历史验收，不与新一轮数量累加。

用户已授权本轮 API 总额不超过 $10。两次已审阅的开发修订保留了上下文容量与请求次数不足导致的失败，当前总分配 $9.44，余量 $0.56。32K 上下文、24 次请求上限的 Flash / Pro 公开 smoke 均已真实独立验收通过，包含前两次失败的开发费用上界为 $0.084483544。72 个 heldout trial 已完成，51 次独立通过、21 次失败。完整结算运行费用上界 $1.030805844，加 5 个待核对运行的完整分配 $0.60，保守计入预算合计 $1.630805844；不称为精确账单。B2 与 guard-off 都通过 10/12，没有证实 guard 效果收益。见 [真实结果与限制](deepseek-live.md)、[独立审计](../reviews/deepseek-live-review.md) 和 [机器状态](../../evals/deepseek-live-status.json)。

开发修订后的 [251 passed、3 skipped](deepseek-amendment-offline-junit.xml) 保留为历史记录。终端费用分类修复后的最终离线检查为 **289 passed、3 skipped**（需要显式 Docker 审阅环境的项目），Ruff 通过，mypy 57 个文件通过；[最终 JUnit](deepseek-final-offline-junit.xml)。另有 [3 项真实 PostgreSQL/rootless 运行时回归](deepseek-incomplete-runtime-junit.xml) 全部通过。修复仅影响后续请求，本次原始结果与账本未改。这些组与此前基线存在重合，不累加计数。

## 当前可运行的结果

公共 Typer CLI 经 FastAPI 提交固定 commit 的修复任务，独立 worker 使用 PostgreSQL 账本和 LangGraph checkpoint 驱动工具执行。Rootless Docker 执行命令，候选补丁在全新、只读的验证工作区接受测试；SSE 支持按持久序号重连，工件通过 owner 校验后下载。演示实际完成了一处复合 cursor 分页修复，原始 checkout 保持不变。

演示中的模型轮次由明确的离线脚本提供。它证明 API、worker、工具、验证与工件链路；实际记录为 50 个连续事件，6 个回归测试、3 个目标测试完成，5 份容器 receipt 均来自 rootless daemon。见 [公开客户端记录](public-client.md) 与 [JSON 工件](public-client.json)。

创新实现集中在版本化 Evidence Ledger：代码片段绑定文件 hash、行范围和原工具调用；测试证据绑定 snapshot/profile；文件修改后，旧证据不能作为当前事实进入后续上下文。该机制已接入工具、检索、压缩与运行时，`B2_guard_off` 为同条件消融开关。本轮 B2 与 guard-off 的任务通过结果相同；stale leak 指标未采集，不能宣称效果提升或零泄漏。

## 实际检查

| 检查 | 观察结果 | 原始证据 |
| --- | --- | --- |
| 单元与原生 SDK 合同 | 152 passed，0 skipped；不访问厂商 API | [offline-junit.xml](offline-junit.xml) |
| PostgreSQL / rootless Docker / 公共客户端 / 独立审阅回归完整组 | 33 passed，0 skipped | [full-integration-junit.xml](full-integration-junit.xml) |
| pytest 独立验收补充 | 4 passed，0 skipped；两个入口通过，全部 skip / 提前 exit 0 被拒绝 | [pytest-verifier-junit.xml](pytest-verifier-junit.xml) |
| Rootless 专项，包含于上述完整组 | 12 项；真实 OOM、PID 拒绝、CPU throttling、取消、worker 退出、只读挂载 | [专项说明](sandbox.md) |
| OOM 内核计数补验 | rootless / rootful 各 1 passed；子进程 SIGKILL，`oom_kill` 正增量，observer 存活 | [rootless](oom-rootless-junit.xml) / [rootful development](oom-rootful-dev-junit.xml) |
| 固定初始迁移 | 独立空数据库 upgrade → metadata check → downgrade → upgrade；11 张业务表及约束一致 | [迁移审计](migration.md) |
| 30 个诊断任务资格 | 60 次 baseline/reference 独立 rootless 检查；30/30 原缺陷失败且参考修复通过 | [qualification.json](../../evals/qualification.json) |
| 静态检查 | Ruff 通过；mypy 在 54 个源码文件中未发现问题 | 本文下方命令 |

测试数量按不同测试集合记录，专项与完整组存在包含关系，不相加重复计算。完整组会运行本地私有 grader 测试；公共克隆缺少私有断言时相关项明确跳过，不能声称获得相同的私有评测结果。

运行环境：Linux、Python 3.13.15、uv 0.12.10、Docker 29.8.0、systemd/cgroup v2 rootless daemon。验收镜像是 `sha256:88ea842ecc6d79db7d22c0cf911ea41b5ebec7c6846298eea1ae7a77dc2c7e15`，其中 pytest 为 8.4.2；项目依赖由 `uv.lock` 冻结。

```bash
uv run --no-sync ruff check src scripts tests examples migrations
uv run --no-sync mypy src scripts/demo.py scripts/check_migration.py
uv run --no-sync pytest tests/unit tests/contract -q

# 先导出专用测试数据库和 rootless socket；不要对业务数据库运行。
env FORGE_TEST_DATABASE_URL="$FORGE_DATABASE_URL" \
  FORGE_TEST_DOCKER_HOST="$FORGE_DOCKER_HOST" \
  FORGE_TEST_IMAGE=forge-runner:dev \
  FORGE_EVAL_PRIVATE_ROOT=.forge/private-evals \
  FORGE_REVIEW_DOCKER=1 FORGE_REVIEW_DOCKER_HOST="$FORGE_DOCKER_HOST" \
  FORGE_RUN_PUBLIC_CLIENT=1 \
  uv run --no-sync pytest tests/integration tests/review -q
```

公共客户端测试需要空闲专用队列，与 [quickstart](../quickstart.md) 中的运行约束相同。Rootless 验收没有启用 rootful fallback。

## 需求证据对应

原始方案的逐条定义保留在 [R01–R33 需求表](../planning/requirements.md)。主要验证入口如下；“有实现”与“真实模型测量完成”分开记录。

| 范围 | 实现与证据 |
| --- | --- |
| 原生 provider、流完整性、续接、费用：R03–R07、R17–R18 | `providers/`、`context/budget.py`；`tests/contract/test_providers.py` 覆盖两个实际 SDK 的 MockTransport、断流、重试、超时、取消、usage。完整组验证无效参数反馈与有限升级，错误调用未进入工具执行。 |
| 工具、上下文、检索、压缩：R10–R16 | `tools/`、`retrieval/`、`context/`；工具 hash/路径边界、tree-sitter 缓存失效、证据往返、过期事实过滤与预算测试。 |
| 账本、恢复、审批、取消：R08–R09、R19–R21 | `runtime/`、`worker.py`、`persistence/`；真实 PG 测试确认已入账模型不重采样、已应用 patch 不重放、checkpoint 审批重启、旧 epoch 拒写、取消先核对 receipt。 |
| API 与客户端：R22–R24 | owner/幂等/SSE/工件合同及独立 API + worker 公共 CLI 演示。 |
| 独立评测：R25–R28 | `evals/` 实现私有投影、sealed snapshot、B0–B3/strong-only/guard-off、JSONL/CSV/HTML 与 task-paired 分析；30 任务资格已验证；DeepSeek 12-task pilot 已完成 72 次真实 trial，51 次通过，原始完整研究仍未执行。 |
| 可观察性与交付：R29–R32 | OpenTelemetry span/local exporter、工件、ADR、独立规划/开发/审阅、测试与 CI 配置。 |
| 可选扩展：R33 | MCP、Web、多语言执行 profile、本地推理等仍为 optional_deferred。 |

独立审阅复现并推动修复了 partial usage 错结算、零测试假成功、运行时篡改回归、工件归属不一致、总时长上限失效；见 [审阅报告](../reviews/independent-review.md)。

## GitHub Actions

以下记录保留早期完整验收的原始证据。DeepSeek 接入提交 `3434e3ff2fe8d7529aa6633fb7a6dbeeb4389fea` 的 [CI 34673716224](https://github.com/JDinSeattle/forge-evidence-agent/actions/runs/34673716224) 两个 job 也已通过；本次修订和结果发布的状态见 [main 分支最新 Checks](https://github.com/JDinSeattle/forge-evidence-agent/actions/workflows/checks.yml?query=branch%3Amain)。

[远程 CI 34200373880](https://github.com/JDinSeattle/forge-evidence-agent/actions/runs/34200373880) 在源码提交 `e2d7e5e092183b92b5c8b30341ab74d76367a230` 上完成，两个 job 均为 success。原始任务元数据保存在 [remote-ci.json](remote-ci.json)，JUnit 为 [offline](remote-offline.xml) 和 [PostgreSQL/rootful development](remote-integration-rootful-dev.xml)。远程验收包含从 lock 安装、静态检查、全新数据库迁移往返、镜像构建和公共客户端演示。

GitHub hosted runner 使用显式 rootful development daemon；该结果没有替代本地 rootless 验收。第一次运行的 OOM 测试观察到进程退出 137、Docker `OOMKilled=false`，未据此推断原因；最终验收直接断言内核 cgroup `oom_kill` 增量、64 MiB/零 swap、子进程 SIGKILL 与 observer 存活。修正后的测试在两种本地 daemon 及远程 CI 均通过。

## 尚未完成的外部验证

- OpenAI、Anthropic 真实账号 smoke：当前未配置本地 provider 凭据，未发送付费请求。
- 原始完整实验仍为 0/540 planned trials；其额外预算尚未授权。新 DeepSeek 预算内 pilot 已冻结模型/能力/价格与 $10 总上限，当前配置的两次 smoke 已通过，72 个 trial 已完成。见 [原始计划](../../evals/live-status.json) 与 [DeepSeek 状态](../../evals/deepseek-live-status.json)。


模型修复结果仅限已执行的 12 个诊断任务；没有稳定成本改善百分比或 production-scale 声明。只读测试探针也不构成对同一 Python 解释器中任意恶意代码的密码学可信执行证明。
