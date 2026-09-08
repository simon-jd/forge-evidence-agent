# 0006: Reserve uncertain spend and compare policies on the same tasks

Status: accepted.

Each provider attempt reserves input plus output at frozen Decimal prices before sending a request. Failed, cancelled and incomplete requests retain conservative estimated/unknown debt. Cancellation prevents future work; it cannot revoke provider charges. The limits apply to attempts, tools, repairs, wall time and money.

RuleRouter uses only visible failures, invalid tool arguments and context capacity. It has a finite escalation count and checks remaining budget. It cannot access hidden targets or reference patches. B3 is compared with a fixed strong model, as well as B0/B1/B2, so a gain caused solely by buying more model capacity is visible.

Evaluation requires a separate explicitly authorized experiment-wide cost ceiling. Trials have immutable IDs; a crashed scheduler marks unfinished work for reconciliation instead of resubmitting a possibly charged trial. All failure spending contributes to cost per successful task. Zero successes makes that ratio undefined. Repeats are grouped by task for paired analysis and bootstrap intervals.

Consequences: unknown debt may stop an otherwise affordable experiment. An estimated price is not a precise bill. Live results are absent until actual authorized trials run; report generation and fake adapters never fill in success rates or savings.
