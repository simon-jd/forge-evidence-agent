# 0002: Budget code context before introducing a vector database

Status: accepted.

File/keyword retrieval and tree-sitter symbols provide exact path, line and content provenance. They are cheaper to inspect and invalidate than an embedding index for the bounded Python repositories supported here. Embeddings remain an optional experiment justified by observed retrieval failures.

ContextBuilder accounts for instructions, tool schemas, exact user constraints, evidence, causal tool batches, summaries and reserved output. The default counter is a conservative UTF-8 byte estimate, explicitly recorded as estimated; it is not advertised as a model tokenizer. Actual provider usage settles a reservation only when the response is confirmed.

B0 uses recent history; B1 adds selected evidence; B2 adds portable compaction. A summary is a navigation aid with references to full artifacts. Pending tool calls and results stay together. If mandatory constraints exceed the budget, the run stops or routes to a sufficient configured capacity instead of silently discarding them.

Consequences: estimates can underuse a large context window and do not prove latency or cost gains. Token-counter calibration and summary quality are separate measurements, not presumed benefits.
