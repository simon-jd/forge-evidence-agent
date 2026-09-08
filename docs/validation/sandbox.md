# Workspace, tools and sandbox development evidence

Independent development Agent: `sandbox_development`. Date: 2026-09-07.

This report records actual local checks of `src/forge/execution`, `src/forge/tools`,
`src/forge/artifacts` and `docker/runner.Dockerfile`. It does not certify the full
API/worker/grader integration or provider quality. The subsequent rootless checks
below verify the execution boundary on the dedicated local daemon.

## Implemented contracts

- `WorkspaceManager(root).prepare(run_id, source, base_commit)` resolves a real
  Git commit and creates separate `worktree`, trusted `baseline` and `control`
  directories. The source checkout and its uncommitted changes are not modified.
  Only `worktree` is mounted into a job container. Run root, baseline and control
  directories are mode `0700`; worktree content supports a non-root container
  writer. `snapshot()` returns file content hashes and `get_diff()` includes
  additions and deletions, including text without a trailing newline.
- `Workspace.fence(epoch)` persists new ownership independently of the workspace
  write lock. It must be called immediately after a successful database lease
  claim. A new epoch does not clear the active-job marker or prove that the prior
  command stopped. Tool operations use cross-process `flock`; reads share it and
  patches/commands take it exclusively.
- `SafeRoot` uses directory descriptors and `O_NOFOLLOW` for reads and writes.
  Parent traversal, absolute paths, control paths, symlinks, hardlinks and special
  files are rejected. Repository/scan/file size limits are explicit. Unsupported
  or inaccessible entries are not silently accepted as a complete snapshot.
- Seven tools are implemented. `read_file` can also reread a retained artifact by
  opaque ID after checking that it belongs to the current run. File evidence is
  bound to content hashes and line ranges; historical log artifacts are not
  labelled as current file evidence. `search_code` gives secured file bytes to
  ripgrep on stdin, so repository paths/configuration cannot direct it elsewhere.
- Unified patches validate every affected path, every expected hash and every
  hunk before writing the first file. A persisted before/after manifest supports
  recovery after all writes completed. Partial or changed after-states raise
  `NeedsReconciliation`; this is ordered application, not a claim of atomic
  multi-file filesystem transactions.
- `DockerExecutor` persists a planned command before launch, uses a stable
  container name and ownership labels, and retains exited containers for
  reconciliation. It never falls back to host command execution. A missing
  original job raises `NeedsReconciliation` rather than repeating the command.
  `cancel()` stops the specifically owned container and confirms its state.
- `execute(..., read_only_workspace=True)` gives a grader a read-only bind mount.
  This flag is part of the operation's argument hash and recorded limits, so an
  existing operation cannot silently change mount policy during replay. Ordinary
  agent tools retain the default writable workspace; trusted grader setup injects
  its files before launching the read-only verification job.
- Command receipts record actual image identity, rootless status, resource
  configuration, timestamps, exit code, retained stdout/stderr, byte counts,
  truncation and workspace revision. Separate Docker log streams drain both
  outputs after the local retention cap. Default local retention is 16 MiB per
  stream; daemon log retention is 64 MiB. Unbounded output is not advertised as
  losslessly archived. Long retained logs can be reread through `read_file`.

## Actual checks

Static checks:

```bash
.venv/bin/ruff check src/forge/execution src/forge/tools src/forge/artifacts \
  tests/unit/test_tools_workspace.py tests/integration/test_sandbox.py
.venv/bin/mypy src/forge/execution src/forge/tools src/forge/artifacts
```

Observed: Ruff passed; mypy reported no issues in 10 source files.

Unit checks:

```bash
.venv/bin/pytest tests/unit/test_tools_workspace.py -q
```

Observed: **26 passed in 0.45 s**. Coverage includes committed snapshot isolation,
source preservation, private control directories, malicious paths, symlinks,
hardlinks and FIFOs, hash-bound read/search evidence, retained artifact rereads,
cross-run artifact denial, all-file patch preflight, protected tests, duplicate
operation identity, competing writes, stale epochs, fully applied patch recovery,
partial-patch refusal, and applying exported added/deleted/no-newline diffs with
real `git apply` against a clean checkout. Unit tests also explicitly verify that
the default executor refuses rootful Docker and missing resource-limit support.

The Codex command sandbox in this session blocked asyncio thread notifications:
the first asynchronous test completed but event-loop teardown waited indefinitely.
Running these same tests with approved command escalation completed normally.
This observation is separate from product runtime evidence.

Initial real container checks (rootful development daemon; historical evidence):

```bash
docker build -f docker/runner.Dockerfile -t forge-runner:dev .
env FORGE_DOCKER_HOST=unix:///var/run/docker.sock \
  FORGE_TEST_IMAGE=forge-runner:dev FORGE_ALLOW_ROOTFUL_TESTS=1 \
  .venv/bin/pytest tests/integration/test_sandbox.py -q
```

Observed: **7 passed, 1 skipped in 6.03 s**. This earlier result was superseded for
rootless acceptance by the full run below. Tested image ID:

```text
sha256:88ea842ecc6d79db7d22c0cf911ea41b5ebec7c6846298eea1ae7a77dc2c7e15
```

The built image contains Python 3.13 and pytest 8.4.2. The image's exact identity,
rather than the mutable development tag, should be frozen for an evaluation run.

### Rootless follow-up: 12 passed, zero skipped

The initial UID-map failure was not a permanent host incompatibility. Direct
Codex execution inherited `NoNewPrivs=1`, which prevented the setuid mapping helper
from installing the subordinate UID map. After explicit authorization, the main
Agent started the standard rootless Docker daemon through the user service
manager with cgroup delegation. The existing default daemon and Docker context
were not changed.

Actual daemon: Docker **29.8.0**, socket
`unix:///tmp/forge-rootless-1000/docker.sock`. Docker reports
`name=seccomp,profile=builtin`, `name=rootless`, `name=cgroupns`, memory/PID limit
support and **systemd / cgroup v2**. The executor also requires CPU quota support
before accepting the daemon; the CPU test below proves actual throttling.

The transient unit's observed startup configuration is reproducible with the
following commands on this project host. The `forge-rootless` unit must not
already be running when issuing the startup command; reuse the existing socket
for subsequent tests.

```bash
mkdir -p /tmp/forge-rootless-1000
chmod 700 /tmp/forge-rootless-1000
systemd-run --user --unit=forge-rootless --collect --property=Delegate=yes \
  --setenv=XDG_RUNTIME_DIR=/tmp/forge-rootless-1000 \
  /usr/bin/dockerd-rootless.sh \
  --host=unix:///tmp/forge-rootless-1000/docker.sock \
  --data-root='/home/postedism/Desktop/mini claude code/forge/.forge/docker' \
  --exec-root=/tmp/forge-rootless-1000/exec \
  --pidfile=/tmp/forge-rootless-1000/dockerd.pid --iptables=false
```

The prebuilt runner was transferred without changing the global Docker context:

```bash
docker --host unix:///var/run/docker.sock save \
  --output /tmp/forge-runner-rootless-transfer.tar forge-runner:dev
docker --host unix:///tmp/forge-rootless-1000/docker.sock load \
  --input /tmp/forge-runner-rootless-transfer.tar
docker --host unix:///tmp/forge-rootless-1000/docker.sock image inspect \
  forge-runner:dev --format '{{.Id}}'
```

The destination image ID is unchanged:
`sha256:88ea842ecc6d79db7d22c0cf911ea41b5ebec7c6846298eea1ae7a77dc2c7e15`.

```bash
env FORGE_TEST_DOCKER_HOST=unix:///tmp/forge-rootless-1000/docker.sock \
  FORGE_TEST_IMAGE=forge-runner:dev FORGE_ALLOW_ROOTFUL_TESTS=0 \
  .venv/bin/pytest tests/integration/test_sandbox.py -q \
  --junitxml=docs/validation/rootless-sandbox-junit.xml
```

Observed at 2026-09-07 16:47 America/Los_Angeles: **12 passed in 8.80 s; zero
skipped**. The persisted [JUnit result](rootless-sandbox-junit.xml) names every
test and records its actual duration. No test used `allow_rootful=True`.
`FORGE_TEST_DOCKER_HOST` is the preferred test override; `FORGE_DOCKER_HOST` and
`DOCKER_HOST` are accepted fallbacks. The production executor's rootless default
was not weakened to make these tests pass.

The passed real-rootless-daemon cases prove:

1. Container UID is 1000, root filesystem writes fail, network connection fails,
   model credentials and Docker socket are absent, and the sole bind mount is
   `/workspace`. Docker reports requested memory/PID/nano-CPU configuration.
   Editing/creating files changes only the independent worktree.
2. One million bytes on each of stdout/stderr are drained; bounded retained output
   has accurate byte counts and a truncation indicator.
3. Timeout stops a container containing a spawned child process. Repeating the
   same operation returns its original container receipt without starting again.
4. Cancellation confirms the original container is no longer running.
5. Removing the receipt simulates the result-persistence window; reconciliation
   reads the original exited container and leaves a side-effect counter at one.
6. Removing both the original container and its receipt refuses replay.
7. A real child worker process exits with `os._exit(29)` immediately after writing
   the running-operation record. The parent observes that record without a
   receipt, rejects a new command while the original job is active, and adopts
   the original container under a newer epoch. Its counter remains one.
8. A subordinate container UID creates a mode-0600 file that the host workspace
   owner cannot read. The operation enters `NeedsReconciliation`; it is not
   silently omitted from a supposedly complete snapshot. This previously skipped
   scenario now passed on the real rootless daemon.
9. At this original local observation, a job limited to **64 MiB** attempted a
   256 MiB allocation. Its receipt recorded exit code 137 and `oom_killed=true`,
   while the daemon remained available for the remaining tests. The OOM test was
   subsequently revised to require direct cgroup kernel evidence, as below.
10. A job limited to **16 PIDs** attempts at most 24 child spawns. It observes
    `EAGAIN` before reaching the profile limit and then explicitly terminates and
    reaps the children it did create. This is a bounded test, not an uncontrolled
    process-spawning workload.
11. A job limited to **0.25 CPU** observes the corresponding `cpu.max` ratio,
    spends 0.3 CPU seconds in a bounded loop, and sees `nr_throttled` increase.
    Measured wall time exceeds twice its CPU time. This tests actual quota
    enforcement rather than only checking Docker configuration fields.
12. A grader workspace bind mount reports `RW=false`; trying to replace a trusted
    test raises an OS error, and the full before/after file snapshots agree.
    Replaying its operation ID with a writable mount request is rejected because
    the argument hash changed.

Only uniquely labelled containers created by each test are removed. Other project
containers and concurrently running graders are outside this test cleanup scope.

### OOM evidence follow-up, 2026-09-08

The first remote rootful development CI invocation reported 28 passing tests,
one rootless-only skip and one failure: the oversized allocation returned
`error`/137, but Docker's reported `oom_killed` value was false. The cause of that
metadata discrepancy has **not been established**. Exit 137 alone is also
insufficient to prove that the configured memory cgroup caused the kill.

The revised memory test keeps a small parent observer inside the 64 MiB cgroup.
It reads `memory.max`, `memory.swap.max` and `memory.events`, then starts one child
that attempts a 256 MiB allocation. The child increases its own `oom_score_adj`
to 1000 to make it the preferred victim while leaving the cgroup limits intact.
After reaping the child, the parent prints the before/after kernel counters and
exits successfully. The test requires all of the following:

- The kernel's memory limit is exactly 64 MiB, swap allowance is zero, and the
  corresponding Docker memory configuration agrees.
- The child was terminated by `SIGKILL` and the same cgroup's `oom_kill` counter
  increased. Neither a plain nonzero exit nor Docker metadata alone satisfies it.
- The parent emitted its observation and exited zero; the Docker daemon still
  responds to a ping.

The main development thread will run this revised test on both the real rootless
daemon and the explicitly selected rootful development daemon. Their new results
are separate from the original local JUnit observation above; no new passing
result is claimed here before those executions.

## Defects found by real execution and corrected

The initial Docker `attach(logs=True)` implementation was unsuitable for recovery:
it could wait for a future start when the container had already exited. Large
output also exposed a stream-demultiplexing failure. The implementation now uses
separate `logs(follow=True)` readers for stdout and stderr; completed-container
recovery and large-output checks passed after this change.

The initial directory creation let an intermediate `control` directory inherit
the default permissions. It now creates that directory explicitly as `0700`, and
the permissions test verifies the resulting filesystem state.

## Explicit remaining boundaries

- Rootless execution and the twelve named scenarios are verified on this local
  daemon. The earlier rootful development results remain labelled as such. The
  transient service is not a tested deployment/upgrade/backup procedure, and
  other hosts still need their own user-namespace and cgroup checks.
- Supported workspaces are bounded UTF-8 source repositories with ordinary files.
  Symlink repositories, binary patch export and arbitrary Git metadata changes
  are outside this implementation. Text hashes do not represent permission-only
  changes; callers should not treat mode-only changes as an exported fix.
- Docker command deadlines are enforced by the active observer; recovery computes
  the remaining deadline from the persisted start time. An absent worker is not
  a daemon-level deadline watchdog. A recovered worker must reconcile active jobs
  before admitting new work.
- Memory/PID exhaustion and CPU throttling are now verified with bounded real
  jobs. These checks do not establish sustained platform throughput or fairness
  between many concurrent tasks.
- Database lease validity remains the control plane's responsibility. Persisting
  an epoch in a workspace is not an alternative to checking the current database
  lease before launching work. Approval policy is likewise enforced by the
  runtime before invoking the dispatcher; repository text cannot grant approval.
- Full API/worker/grader integration and independent security review remain
  separate requirements. Ordinary Docker isolation is not a claim of absolute
  safety against arbitrary hostile native code.
