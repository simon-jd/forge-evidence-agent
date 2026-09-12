# 实现与验证状态

更新日期：2026-09-11。项目范围是 AI Engineer 方案，位于独立的 `forge/` 子目录；没有把另一份 SDE 方案的可选组件混入主实现。以下结果来自本项目实际执行，不代表公司经历或真实模型修复能力。

## DeepSeek 与累计预算补充（2026-09-11）

已接入 DeepSeek Responses API、独立凭据、完整请求预留和峰值费率上界口径；实际 PostgreSQL/rootless Docker 集成使用模拟 HTTP 验证生产 factory 与 read → patch → finish → 独立验收。新一轮单元/协议 **202 passed**，完整集成/审阅 **42 passed**，均无跳过；合计 244 项不同测试。Ruff 通过，mypy 在 57 个文件中通过。详见 [DeepSeek 验证记录](deepseek.md)。下表保留 9 月 8 日历史验收，不与新一轮数量累加。

用户已授权本轮 API 总额不超过 $10。固定 pilot 分配 $9.04，包含两个公开 smoke 和 72 个 heldout trial；余量 $0.96 不自动使用。持久总账、未知费用不释放与重复运行拒绝已实现。当前缺少本地 DeepSeek 凭据，全部 74 个 slot 仍 pending，付费请求为 0、支出为 $0、真实质量未测量。见 [可执行方案](../evaluation-deepseek.md) 与 [机器状态](../../evals/deepseek-live-status.json)。

## 当前可运行的结果

公共 Typer CLI 经 FastAPI 提交固定 commit 的修复任务，独立 worker 使用 PostgreSQL 账本和 LangGraph checkpoint 驱动工具执行。Rootless Docker 执行命令，候选补丁在全新、只读的验证工作区接受测试；SSE 支持按持久序号重连，工件通过 owner 校验后下载。演示实际完成了一处复合 cursor 分页修复，原始 checkout 保持不变。

演示中的模型轮次由明确的离线脚本提供。它证明 API、worker、工具、验证与工件链路；实际记录为 50 个连续事件，6 个回归测试、3 个目标测试完成，5 份容器 receipt 均来自 rootless daemon。见 [公开客户端记录](public-client.md) 与 [JSON 工件](public-client.json)。

创新实现集中在版本化 Evidence Ledger：代码片段绑定文件 hash、行范围和原工具调用；测试证据绑定 snapshot/profile；文件修改后，旧证据不能作为当前事实进入后续上下文。该机制已接入工具、检索、压缩与运行时，`B2_guard_off` 为同条件消融开关。是否改善模型效果仍等待真实实验。

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
| 独立评测：R25–R28 | `evals/` 实现私有投影、sealed snapshot、B0–B3/strong-only/guard-off、JSONL/CSV/HTML 与 task-paired 分析；30 任务资格已验证，真实模型 trial 为 0。 |
| 可观察性与交付：R29–R32 | OpenTelemetry span/local exporter、工件、ADR、独立规划/开发/审阅、测试与 CI 配置。 |
| 可选扩展：R33 | MCP、Web、多语言执行 profile、本地推理等仍为 optional_deferred。 |

独立审阅复现并推动修复了 partial usage 错结算、零测试假成功、运行时篡改回归、工件归属不一致、总时长上限失效；见 [审阅报告](../reviews/independent-review.md)。

## GitHub Actions

[远程 CI 34200373880](https://github.com/JDinSeattle/forge-evidence-agent/actions/runs/34200373880) 在源码提交 `e2d7e5e092183b92b5c8b30341ab74d76367a230` 上完成，两个 job 均为 success。原始任务元数据保存在 [remote-ci.json](remote-ci.json)，JUnit 为 [offline](remote-offline.xml) 和 [PostgreSQL/rootful development](remote-integration-rootful-dev.xml)。远程验收包含从 lock 安装、静态检查、全新数据库迁移往返、镜像构建和公共客户端演示。

GitHub hosted runner 使用显式 rootful development daemon；该结果没有替代本地 rootless 验收。第一次运行的 OOM 测试观察到进程退出 137、Docker `OOMKilled=false`，未据此推断原因；最终验收直接断言内核 cgroup `oom_kill` 增量、64 MiB/零 swap、子进程 SIGKILL 与 observer 存活。修正后的测试在两种本地 daemon 及远程 CI 均通过。

## 尚未完成的外部验证

- OpenAI、Anthropic 真实账号 smoke：当前未配置本地 provider 凭据，未发送付费请求。
- 原始完整实验仍为 0/540 planned trials；其额外预算尚未授权。新 DeepSeek 预算内 pilot 已冻结模型/能力/价格与 $10 总上限，当前为 0/72 trials、0/2 smoke，等待本地凭据。见 [原始计划](../../evals/live-status.json) 与 [DeepSeek 状态](../../evals/deepseek-live-status.json)。


没有模型修复率、成本改善百分比或 production-scale 声明。只读测试探针也不构成对同一 Python 解释器中任意恶意代码的密码学可信执行证明。
