# WebSocket quality observations (Ops)

The new Analysis metric `websocket-session-quality` is explicitly in `spec.dryRun`.
It runs alongside existing jobs at the existing 25/50/100 steps. It cannot abort a
rollout through its Analysis verdict. Readiness/Sync/progress checks still apply.
No additional latency or throughput gates have been added.

Three independently evaluated signals are exported with `role`, `revision`, and
`signal` labels under `chargeops_canary_ws_quality_{value,samples,limit,status}`:

| Signal | Meaning | ConfigMap threshold |
| --- | --- | --- |
| disconnect_percent | abnormal + handler_error closes / ordinary completed closes, percent; excludes drain and fixtures | WS_DISCONNECT_MAX_PERCENT |
| reconnect_success_percent | same-Pod successful recovery cohorts / (success + observed timeout), percent | WS_RECONNECT_SUCCESS_MIN_PERCENT |
| reconnect_p95_seconds | histogram p95 of successful same-Pod recoveries within observation deadline | WS_RECONNECT_P95_MAX_SECONDS |

All three thresholds default to **empty**. The example values in the conversation
are not silently installed as production thresholds. `WS_QUALITY_MIN_SAMPLES`
defaults to 10, a configurable observation floor, not a validated statistical guarantee.
Queries use a five-minute window. Stable and Canary are selected by their Service
revision hashes. Stable is omitted at the existing final step (`check-stable=false`).
There is no automatic relative Stable-to-Canary cutoff. Values are compared visually;
configured absolute thresholds assess each observed role independently.

Statuses: 1=threshold passed; -1=threshold violated; 2=missing/insufficient samples;
3=measured but threshold not configured. UNVERIFIED/OBSERVE jobs exit nonzero so
Argo records truthful dry-run evidence instead of a fake PASS. Logs include JSON
for every signal, with values, samples, threshold, role and revision where available.
One missing signal does not hide other signals. Prometheus errors leave that signal
unverified. No `or vector(0)` substitutes missing observations.

Application prerequisite: deploy the CSMS version providing
`chargeops_csms_recovery_outcomes_total`, `chargeops_csms_recovery_seconds`, and
`chargeops_csms_recovery_discarded_total`. Old Stable images cannot provide these
new metrics, so the first migration can show Stable UNVERIFIED.

The same-Pod scope is deliberate. It does not measure full client retry success or
cross-Pod recovery. Successful latency excludes timed-out cohorts: evaluate the
success rate and counts alongside p95. Capacity loss makes recovery signals unverified.
Runtime crashes/API failures still follow the existing operational controls.

Deployment order: build/review CSMS instrumentation, review GitOps changes, deploy
through the normal pipeline, import `chargeops-canary-live-v18.json` into Grafana.
Do not Sync this branch directly during a different approved candidate's pipeline:
its verified policy fingerprint would no longer match. No cluster Sync was performed
by the local implementation work.
