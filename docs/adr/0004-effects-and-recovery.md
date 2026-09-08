# 0004: Receipts define recoverable effects; checkpoints define control flow

Status: accepted.

PostgreSQL owns the run lease, monotonically increasing epoch, durable model/tool ledger and ordered events. LangGraph's PostgreSQL checkpointer owns graph control position. Every replayed node reads the business stage first, and a complete recorded model response is reused before issuing another request.

A tool's identity includes run, logical turn and ordinal. Equal arguments in different turns are not automatically deduplicated. A planned tool row precedes execution. Named Docker containers and on-disk operation manifests survive worker death; a completed receipt precedes the result transaction. Patch recovery checks every expected after-hash. A partial patch or missing container receipt is an explicit reconciliation pause, not an invitation to retry.

A new database epoch cannot stop an existing shell. Workspace fencing and a shared cross-process lock prevent new writes from overtaking the previous job. A successor queries the same job; cancellation waits for a terminal container observation. Approvals bind exact arguments, profile policy and workspace revision, and are checked again at execution.

Consequences: storage corruption and arbitrary commands do not have an exactly-once guarantee. A lost observer can delay timeout enforcement until recovery. Storage backup, distributed sandbox placement and automatic reconciliation of unknown writes are outside this single-host contract.
