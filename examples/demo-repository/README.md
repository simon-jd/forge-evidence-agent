# Event replay cursor defect

This is an intentionally broken, public demonstration repository. It models an
event consumer reconnecting with a composite `(timestamp_ms, sequence)` cursor.
The initial implementation loses remaining events sharing the last event's
millisecond when a page boundary falls inside that timestamp.

Run `python -m unittest test_cursor` to reproduce the failure. The task is to fix
`replay.py`, preserve the public API, and leave these acceptance tests unchanged.

`scripts/demo.py` copies this repository into a separate committed source checkout.
Its default offline configuration contains an explicitly authored sequence of
tool calls, including a known correction. That demonstration tests Forge's public
client and execution machinery; it is not evidence of a model discovering a fix.

The generated fixture repository uses fixed author/committer identity and a fixed
2026-09-07 timestamp so its baseline commit is reproducible. These are synthetic
fixture metadata, not Forge's development commits, work dates or employment
history. No project history is constructed by this demonstration.
