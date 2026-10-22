# 앱 이름 통합

앱 저장소와 GitOps의 이미지·서비스·리소스·폴더명에서 앱 버전 접미사를 제거했다. 현재 작업 브랜치는 두 저장소 모두 `kim_chanju`이며 원격 반영과 클러스터 적용은 별도 단계다.

## 현재 구조

- Base 경로: `chatops-bot`, `ai-gateway`, `csms`, `notifier`, `vcharger`, `collector`.
- 이미지: `chargeops-chatops-bot`, `chargeops-ai-gateway`, `chargeops-csms`, `chargeops-notifier`, `chargeops-vcharger`, `chargeops-collector`.
- Deployment 5개와 Collector CronJob 3개, 고유 이미지 6개를 렌더링한다.
- CSMS HTTP·OCPP 주소는 `http://csms:8000`, `ws://csms:9000/ocpp`다.
- Image Updater의 이름·Kustomize 대상도 같은 이미지 이름을 사용하며 digest/Git write-back 설정은 유지한다.
- namespace `ops`, Argo CD Application 이름·Git main·overlay 경로는 유지한다.

## 충돌 처리

기존 `base/chatops-bot`과 이름이 겹치는 다른 bot Base는 기존 폴더를 우선하여 병합했다. Deployment 이름, selector·Pod label, replica 1, Recreate, 보안 설정·리소스 한도, 기존 Secret·ConfigMap 참조는 유지했다. 중복 Kustomize 리소스·이미지와 Image Updater 항목은 기존 항목 하나만 남겼다.

기존 Deployment의 `python -m app.bot`은 새 이미지에 없는 모듈이므로 `python -m chatops_bot.app`으로 맞췄다. DB·CSMS API를 사용하는 앱에 맞춰 `chargeops-app-secrets`를 추가하고 backend URL을 명시했다. 공통 Secret을 먼저 읽고 기존 Secret을 뒤에서 읽어 중복 키는 기존 값이 우선한다. 실제 Secret 값은 변경하거나 저장하지 않았다.

이제 빌드하지 않는 alert-router는 활성 overlay와 Image Updater에서 제외했다. 기존 Base 폴더는 보존한다. CSMS·notifier의 replica 0과 Collector CronJob suspend 설정은 유지했다.

## 적용 순서

1. 앱 저장소 루트 Jenkinsfile로 이미지 6개의 새로운 이름과 `dev` 태그를 먼저 Build/Push한다. 같은 이름으로 이전 bot 이미지를 발행하는 옛 Job은 동시에 실행하지 않는다.
2. bot의 `slack-credentials`, `bot-backend-credentials`, `chargeops-app-secrets`, `chatops-config`를 확인한다. DB_DSN·CSMS_API_TOKEN 등 앱이 필요로 하는 키를 준비한다. CSMS와 notifier는 준비 전 replica 0을 유지한다.
3. 리소스 이름 변경은 Kubernetes의 기존 리소스 이름을 변경하는 작업이 아니다. 새 Deployment·Service·CronJob·PVC가 생성될 수 있으므로 이전 리소스 목록을 확인한다.
4. 특히 Collector PVC 이름이 바뀐다. 이전 PVC에 데이터가 있으면 백업·복원 또는 적절한 볼륨 이전 계획을 먼저 적용한다. 새 PVC만 만들어도 이전 데이터가 자동으로 연결되지는 않는다. 이전 PVC는 데이터 확인 전 삭제하지 않는다.
5. GitOps 변경을 main에 반영한다. Argo CD의 `prune: false`는 유지했다. Sync 후 이전 이름의 workload나 제외된 router가 자동 삭제되지는 않으므로 변경 diff를 기준으로 대상만 별도로 정리한다. 불필요한 중복 Collector 일정은 활성화하지 않는다.
6. 렌더링된 새 이미지 주소, Pod Events, imageID digest와 AI gateway의 `/cicd-healthz` marker `ci-cd-probe`를 확인한다. bot 역할·승인·CSMS 호출과 Collector 데이터 흐름도 서비스 준비 후 확인한다.

## 검증 범위

`kubectl kustomize chargeops-chatops/overlays/dev`와 `scripts/validate-registry.sh`로 리소스 렌더링·Registry 접두사를 확인한다. 중복 리소스 ID, 이미지 항목, Image Updater 이미지·alias, Service selector·backend URL 및 PVC·ConfigMap 참조도 확인한다. 실제 이미지 존재 여부, Secret 값, 클러스터 rollout과 데이터 이전은 로컬 검증에 포함하지 않는다.
