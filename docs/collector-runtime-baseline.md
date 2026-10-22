# Collector 운영 설정 복원

DB팀이 전달한 Downloads의 `configmap.yaml`, `cronjobs.yaml`을 Collector Base에 반영했다. Secret 값이나 Secret 리소스는 포함하지 않는다.

- 이미지: `registry.chargeops.lab:5000/chargeops/collector:0.2` 고정. Collector Image Updater 항목은 제외한다.
- 환경변수: `collector-config`, 기존 `ops/collector-secret` 참조.
- 노드: `kubernetes.io/hostname: k8s-w02`.
- CA: 기존 `chargeops-ca` ConfigMap을 `/etc/chargeops-ca`에 읽기 전용 마운트한다. `ca.crt` 키가 필요하다.
- S3: 버킷 `chargeops-raw`, 엔드포인트 `https://10.10.40.30`, `AWS_CA_BUNDLE=/etc/chargeops-ca/ca.crt`.
- status: 5분마다 실행, info: 매일 03:10 실행. 시간대는 Asia/Seoul이며 둘 다 활성화한다. check는 중지 상태다.
- 원본처럼 Pod securityContext를 지정하지 않는다. 기존 PVC 파일 소유자와의 호환성을 확인한다.
- PVC `collector-raw` 및 StorageClass `local-path`는 변경하지 않는다.

## 적용 전후 확인

1. 기존 `collector-secret`, `chargeops-ca`의 존재와 CA 키, 이미지 pull 가능 여부를 관리 서버에서 확인한다. 값을 저장소에 복사하지 않는다.
2. `k8s-w02` 노드의 hostname label 및 기존 PV의 nodeAffinity가 호환되는지 확인한다. local-path PVC를 삭제하거나 새 볼륨으로 교체하지 않는다.
3. `kubectl kustomize chargeops-chatops/overlays/dev | kubectl apply --dry-run=server -f -`로 기존 리소스에 대한 변경을 검증한다.
4. Image Updater YAML은 앱 overlay 밖에 있으므로 해당 리소스를 관리하는 경로로 별도 반영해야 Collector 추적 제외가 실제 클러스터에도 적용된다. 전체 Image Updater 중지는 별도 운영 작업이다.
5. Argo CD Sync 후 다음 status 실행 로그와 DB·S3 저장 결과를 확인한다. info는 다음 03:10 실행 시 확인하거나 중복 실행 여부를 확인한 뒤 관리자가 수동 실행한다.

로컬에서는 렌더링과 원본 일치만 확인했다. 실제 Secret·CA·PV의 상태와 DB·S3 연결은 클러스터 검증이 필요하다.

두 원본 파일에는 CSMS Deployment가 없으므로 `csms-app-test` 마운트와 affinity/topologySpread는 확인되지 않았다. CSMS 백업의 해당 설정을 확보한 뒤 별도로 반영한다.
