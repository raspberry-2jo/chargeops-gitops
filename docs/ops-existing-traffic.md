# 기존 충전기 지표로 Ops Canary 검증

Ops 기본 배포는 별도 충전기 ID나 접속 프로그램을 요구하지 않는다.
기존 충전기의 CSMS 지표를 Prometheus에서 읽고 AnalysisRun으로 판정한다.
`websocket-ocpp-smoke`는 기본 AnalysisTemplate에서 제외했다.
`configure-ops-ocpp.py`와 release_probe 코드는 향후 선택 검사용이며 기본 승격에는 필요 없다.

## 배포 흐름

kim_chanju Push → Jenkins 테스트·이미지 빌드 → Dev 배포·검증 → PR 생성 →
다른 담당자의 최신 변경 승인 및 merge → 같은 Job에서 동일 digest를 Ops GitOps에 기록 →
Ops 선택 동기화 → Canary 25/50/100% 및 단계별 분석.

## 실패 및 미검증 조건

- 기존 L1~L5 인프라·앱/DB·연결 감소·처리량·오류율 기준은 유지한다.
- 후보 Pod에서 비정상 연결 종료를 관측하면 즉시 실패한다.
- 각 단계 최소 60초 관측 후 후보 Pod 이름의 hash로 범위를 제한한다.
- 최근 1분 Heartbeat 성공 처리량이 분당 1건 미만이거나 필수 지표가 없으면 미검증으로 중단한다.
- 후보 Pod의 최근 1분 OCPP 오류·시간 초과 비중이 5%를 초과하면 실패한다.
- Stable의 Heartbeat는 후보 검증 표본을 대신하지 않는다.

이는 서버 핸들러의 성공 처리 계측이다. 충전기가 응답을 실제 수신했다는 종단 간 증거,
전체 DB 세션 정합성 또는 WebSocket Ping/Pong 검증으로 설명하지 않는다.
기존 장시간 WebSocket 세션은 Canary 비중을 변경해도 자동 이동하지 않는다.
따라서 신규 연결이 Canary에 도달하지 않으면 미검증으로 멈출 수 있다.
Ops 승격에서 시험 ID 요구만 제거하며 승인·검증 조건을 우회하지 않는다.

## 적용 및 확인

새 Jenkins 빌드가 변경된 GitOps 정책을 Dev에서 검증한 후 승인받아 배포한다.
이전 빌드가 승인을 기다리는 중에 정책만 바꾸면 fingerprint 검증으로 중단될 수 있다.

```bash
kubectl -n ops get rollout csms
kubectl -n ops get analysisrun --sort-by=.metadata.creationTimestamp
kubectl -n ops describe analysisrun <최신_AnalysisRun_이름>
kubectl -n ops logs job/<five-level-service-gate_검사_Job_이름>
```

Grafana는 `chargeops-websocket-overview-ko.json` 새 버전을 가져와 namespace를 ops로 선택한다.
상단은 실제 서비스 지표, Canary 상세는 최근 단계 검사 결과이다.
‘새 Canary Heartbeat 성공’과 ‘새 Canary OCPP 오류·시간 초과’도 확인한다.
