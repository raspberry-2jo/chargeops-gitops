# Experiment 1 — Dev WS Canary policy revision (2026-10-31)

2026-11-01 scheduling update: see [experiment 2 decisions](ws-canary-experiment-2-decisions.md).
The current local observation-mode patch is not evidence of remote deployment.
Defer applying the earlier copy/paste guide until experiments 3 and 4 finish;
freeze whichever server revision is actually used for those experiments.

Evidence: user-supplied experiment 1 result table. Raw time series and the 8-second
detection claim were not independently reproduced against a cluster.

| Signal | Dev decision | Evidence / limitation |
|---|---|---|
| Connection decrease | Fail above 2%, after 60 continuous seconds without drain/membership change | Normal worst 0.115%; injected fault 4.375%; transition spike 7.6% is excluded |
| Available replicas | Fail below 4, including during drain | User observed 3 during fault; reported detection 8s is an observation, not a guaranteed SLA |
| OCPP error/timeout | Fail above 1%, subject to sample/coverage gates | Retained experimental candidate; normal and fault were both 0%, so fault sensitivity is unvalidated |
| Unexpected close | Fail above 1%, subject to sample/coverage gates | Retained experimental candidate; graceful Pod termination does not validate abnormal-close detection |
| Server Heartbeat ratio | Observe only; display 99% candidate | User reports 100% real success but 96.6% evaluator result. Do not lower the threshold to hide the discrepancy; counter/window attribution needs investigation |
| Normalized throughput decrease | Observe only; display 15% candidate | Normal variation reached 22.9%; no valid fault comparison. Do not invent a 25/30% failure threshold |
| Reconnect p95 | Observe only, 10s candidate, no invented measurement | Client instrumentation is still required |
| OOM / candidate restart / health | Existing failures remain enforced | Not relaxed by this experiment |

The 60s steady-state exclusion, two non-overlapping passing windows at least 60s
apart, minimum OCPP samples 600, session opportunities 300, freshness checks and
observation deadlines remain unchanged. The settings do not promise completion
within a fixed number of seconds. Heartbeat sample floor 300 remains diagnostic
while that signal is observation-only.

Missing/NaN/undersampled **enforced** signals remain UNVERIFIED. Missing observation
signals remain individually UNVERIFIED and do not block the enforced decision.
SUCCESS therefore means the enforced subset passed; it does not certify the two
observed ratios or reconnect latency. Metrics now carry `mode="enforce"` or
`mode="observe-only"`; dashboard queries for blocking signals should filter on
`mode="enforce"`. The individual state/value/limit are preserved for diagnosis.

## Scope

Only `dev-ws-lab/settings.yaml` opts in with `WS_HEARTBEAT_MODE: observe-only` and
`WS_THROUGHPUT_MODE: observe-only`. Shared policy/evaluator copies support the mode
and remain byte-identical. Missing mode defaults to `enforce`, preserving Ops
behavior; no Ops settings, Rollout strategy, HPA, or live cluster changed.

## Apply and verify

Finish or recover the current experiment before changing evaluator/settings:
in-flight evaluation correctly becomes UNVERIFIED on policy changes. Do not
promote a failed analysis to bypass it. Update the Dev probe ConfigMap and patch
only the two Dev settings keys, then restart only `csms-ws-lab-evaluator` to reload
Python modules. A normal ConfigMap `subPath` mount does not reload running code.
Keep the existing baseline stamp when deploying the patch, and establish a fresh
baseline using the normal lab start workflow for the next experiment.

The copy/paste HTML delivered with this revision includes these exact files and
commands, backs up the local source and the two live ConfigMaps, and leaves the
CSMS Rollout template alone (including the live topology Honor correction).
Record the same topology correction in GitOps separately before a full reapply.

Rollback: restore the saved local source files and only `data` from the saved
ConfigMaps, restart the evaluator, then start a fresh experiment. Do not restore
old ConfigMap metadata/resourceVersion or reuse an old experiment baseline.

## Validation

Policy replay covers normal worst-case readings, transition exclusion, 4.375%
steady loss, availability failure, missing observations vs missing enforced
samples, invalid modes, and continued error/unexpected-close enforcement.
Existing default-mode tests ensure Ops thresholds still fail as before.
No live Dev fault injection or Ops synchronization was performed here.
