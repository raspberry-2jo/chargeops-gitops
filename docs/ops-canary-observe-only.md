# Ops Canary 임시 관찰 모드

2026-10-28 요청에 따라 부하 시험 전에는 검사 결과로 카나리를 중단하지 않도록 한다.
검토 예정일은 2026-10-29이다. 날짜 도달만으로 자동 복원되지는 않는다.

`overlays/canary/analysis.yaml`의 `spec.dryRun`에 다음 두 검사를 명시했다.

- `availability-error-latency-restarts`: HTTP·DB 응답과 Pod 안정성 검사
- `five-level-service-gate`: L1~L5 및 실제 Canary Heartbeat·오류·연결 검사

Argo Rollouts는 해당 검사들을 실행하고 실제 개별 검사 결과와 dry-run summary를 남기지만,
이 검사 실패·미검증만으로 Rollout을 중단하지 않는다. 기존 임계값·probe 코드·메트릭은 변경하지 않았다.
Grafana에 빨간 검사값이나 Job 실패가 보여도 이 기간에는 배포 차단을 뜻하지 않는다.
검증을 통과했다고 표현하지 않고 **관찰 모드로 배포 진행**이라고 해석해야 한다.

이 설정은 모든 배포의 성공을 보장하지 않는다. Pending·이미지 Pull 실패·Pod Ready 부족,
Rollout 진행 제한 시간, Argo CD Sync·Kubernetes API·Git 권한·Jenkins 오류는 여전히 실패할 수 있다.
Dev 통합 검사·PR 승인 조건과 digest 일치 검증도 유지된다.

## 적용

GitOps main 반영 후 **새 Jenkins 실행**이 최신 정책을 포함해 Dev 검증·승인을 거쳐
Ops 선택 Sync를 요청해야 한다. 승인 중인 기존 실행은 정책 fingerprint 변경으로 중단될 수 있다.
이미 생성된 AnalysisRun이나 과거 실패가 자동으로 성공으로 바뀌지는 않는다.
기존 Rollout이 Degraded/abort 상태이면 이전 정상 버전의 복구를 먼저 확인해야 한다.

mgmt의 읽기 전용 확인:

```bash
kubectl -n ops get analysistemplate csms-availability -o jsonpath='{.spec.dryRun}{"\n"}'
kubectl -n ops get analysisruns --sort-by=.metadata.creationTimestamp
kubectl -n ops describe analysisrun <새-AnalysisRun>
kubectl -n ops get rollout csms
```

새 AnalysisRun의 `spec.dryRun`과 `Dry Run Summary`를 확인한다.
클러스터에 직접 Sync하거나 강제 promote하는 작업은 이 저장소 변경에 포함되지 않는다.

## 부하 시험 이후 복원

1. Job 로그와 배포 전후 연결·처리량·오류 데이터를 확인한다.
2. 필요한 실제 임계값과 표본·관측 시간 조건을 수정하고 검증한다.
3. `analysis.yaml`의 `spec.dryRun`과 두 `chargeops.io/analysis-*` 주석을 제거한다.
4. 새 Jenkins 실행으로 정책을 검증·승인·Sync한다.
5. 새 AnalysisRun에서 dryRun이 없는지, 장애 주입 시 실패가 실제 중단으로 이어지는지 확인한다.

빠르게 원래 차단 정책으로 돌아갈 때는 관찰 모드 변경 커밋을 revert하면 된다.
참고: https://argoproj.github.io/argo-rollouts/features/analysis/#dry-run-mode
