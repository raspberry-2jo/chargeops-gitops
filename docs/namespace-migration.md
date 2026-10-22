# 앱 namespace를 ops로 전환

앱 배포 namespace를 `ops`로 통일했습니다.

- `chargeops-chatops/overlays/dev/kustomization.yaml`의 `namespace: ops`
- `chargeops-chatops/argocd/application.yaml`의 `spec.destination.namespace: ops`
- 문서의 namespace 생성, Secret 생성, 조회, rollout, port-forward 및 Job 실행 명령

Overlay가 생성하는 리소스 17개는 모두 `ops`에 배포됩니다.
Argo CD Application과 ImageUpdater는 `argocd`에 유지합니다.
이미지 경로의 `/chargeops/`, 앱·리소스·Secret 이름 및 DB 이름은 유지합니다.

## 기존 클러스터에서 전환할 때

Secret별 키, 필수 여부와 생성 명령은 [Kubernetes Secret 준비 목록](kubernetes-secrets.md)을 참고합니다.

namespace 변경은 기존 리소스의 이름 변경이나 데이터 이동을 수행하지 않습니다.
기존 namespace를 사용 중이라면 자동 Sync 전에 다음 사항을 준비합니다.

1. `ops` namespace에 앱 Secret과 외부 ConfigMap을 준비합니다.
   대상은 `chargeops-app-secrets`, `slack-credentials`, `bot-backend-credentials`,
   `router-credentials` Secret과 `chatops-config` ConfigMap입니다.
   `chatops-config.CSMS_URL`은 기존 bot/router와 API가 호환되는 실제 CSMS 주소로 설정합니다.
   사용하는 서비스에 필요한 값이 있어야 하며 기존 namespace에서 자동 공유되지 않습니다.
   레지스트리는 익명 접근이므로 이미지 pull Secret은 필요하지 않습니다.
2. 기존 Collector PVC에 데이터가 있다면 `ops/collector-v2-raw`로 사용할 스토리지와 데이터 이전을 별도로 준비합니다.
   새 namespace에 같은 이름의 PVC를 생성해도 기존 데이터가 자동으로 옮겨지지 않습니다.
3. 외부 모니터링, RBAC, NetworkPolicy 및 전체 Service DNS를 쓰는 호출자가 있다면 `ops`를 허용하도록 맞춥니다.
4. 기존 앱과 새 앱이 동시에 실행되지 않도록 전환 시점을 조정합니다.
   현재 Application은 `prune: false`이므로 기존 namespace 리소스가 자동 삭제된다고 가정하지 않습니다.
   특히 Slack bot·알림 프로세스와 Collector의 중복 실행을 확인합니다.
   기존 리소스의 정리와 데이터 삭제는 별도 확인 후 진행합니다.

관리 VM에서 namespace를 준비하고 렌더링 및 서버 검증을 수행합니다.

```bash
kubectl create namespace ops --dry-run=client -o yaml | kubectl apply -f -
# Secret/ConfigMap/스토리지 준비 후
sh scripts/validate-registry.sh
kubectl apply --dry-run=server -f artifacts/chargeops-dev.yaml
kubectl apply --dry-run=server -f chargeops-chatops/argocd/application.yaml
# GitOps 반영 및 전환 준비 완료 후 Application의 목적지 변경을 적용
kubectl apply -f chargeops-chatops/argocd/application.yaml
kubectl -n ops get deployment,pod,service,cronjob,pvc
```

현재 작업은 저장소 설정 변경과 로컬 렌더링 검증까지입니다.
클러스터 리소스 이동·삭제, Secret 생성, PVC 데이터 이전 및 실제 배포는 수행하지 않았습니다.
저장소에서 제거한 리소스도 `prune: false` 설정으로 인해 클러스터에 남아 있을 수 있으므로,
사용하지 않는 기존 Deployment·Service와 외부에서 생성한 전용 Secret은 운영환경에서 확인 후 정리합니다.
