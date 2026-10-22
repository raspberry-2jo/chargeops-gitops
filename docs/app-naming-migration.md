# 앱 이름 통합

GitOps의 이미지·서비스·리소스·폴더명에서 앱 버전 접미사를 제거했다. 현재 GitOps 작업 브랜치는 `main`이며 원격 반영과 클러스터 적용은 별도 단계다. 이번 변경에서는 앱 저장소를 수정하지 않았다.

## 현재 구조

- Base 경로: `chatops-bot`, `ai-gateway`, `csms`, `notifier`, `vcharger`, `collector`.
- 이미지: `chargeops-chatops-bot`, `chargeops-ai-gateway`, `chargeops-csms`, `chargeops-notifier`, `chargeops-vcharger`, `chargeops-collector`.
- Deployment 5개와 Collector CronJob 3개, 고유 이미지 6개를 렌더링한다.
- CSMS HTTP·OCPP 주소는 `http://csms:8000`, `ws://csms:9000/ocpp`다.
- Image Updater의 이름·Kustomize 대상도 같은 이미지 이름을 사용하며 digest/Git write-back 설정은 유지한다.
- namespace `ops`, Argo CD Application 이름·Git main·overlay 경로는 유지한다.

## 충돌 처리

이름이 겹치는 bot Base는 버전 표기가 붙어 있던 파일 내용을 우선하여 `base/chatops-bot`으로 통합했다. 해당 원본의 selector·Pod label, replica 0, Recreate, 보안 설정·리소스 한도와 `chargeops-app-secrets` 참조를 그대로 유지하고 이름만 정리했다. 중복 Kustomize 리소스·이미지와 Image Updater 항목도 같은 우선순위로 하나만 남겼다.

이전 bot의 `app.bot` command, 기존 Secret·ConfigMap 참조와 replica 1 설정은 가져오지 않았다. bot은 이미지의 기본 명령 `python -m chatops_bot.app`으로 실행하며 원본에 있던 CSMS·AI·Jenkins URL 중 CSMS 서비스 이름만 정리했다. 실제 Secret 값은 변경하거나 저장하지 않았다.

이제 빌드하지 않는 alert-router는 활성 overlay와 Image Updater에서 제외했다. 기존 Base 폴더는 보존한다. bot·CSMS·notifier의 replica 0과 Collector CronJob suspend 설정은 유지했다.

## 적용 순서

1. 앱 저장소 루트 Jenkinsfile로 이미지 6개의 새로운 이름과 `dev` 태그를 먼저 Build/Push한다. 같은 이름으로 이전 bot 이미지를 발행하는 옛 Job은 동시에 실행하지 않는다.
2. bot·CSMS·notifier의 `chargeops-app-secrets`를 확인한다. DB_DSN·CSMS_API_TOKEN·Slack 토큰 등 앱이 필요로 하는 키를 준비한다. 이 세 서비스는 준비 전 replica 0을 유지한다.
3. 리소스 이름 변경은 Kubernetes의 기존 리소스 이름을 변경하는 작업이 아니다. 새 Deployment·Service·CronJob·PVC가 생성될 수 있으므로 이전 리소스 목록을 확인한다. 특히 기존 `chatops-bot` Deployment와 새 원본의 selector는 다르며 Deployment selector는 수정할 수 없다. 해당 기존 Deployment만 계획된 재생성 대상으로 처리한 뒤 Sync한다. 전역 Force/Replace나 자동 prune 설정을 켜지는 않았다.
4. 특히 Collector PVC 이름이 바뀐다. 이전 PVC에 데이터가 있으면 백업·복원 또는 적절한 볼륨 이전 계획을 먼저 적용한다. 새 PVC만 만들어도 이전 데이터가 자동으로 연결되지는 않는다. 이전 PVC는 데이터 확인 전 삭제하지 않는다.
5. GitOps 변경을 main에 반영한다. Argo CD의 `prune: false`는 유지했다. Sync 후 이전 이름의 workload나 제외된 router가 자동 삭제되지는 않으므로 변경 diff를 기준으로 대상만 별도로 정리한다. 불필요한 중복 Collector 일정은 활성화하지 않는다.
6. 렌더링된 새 이미지 주소, Pod Events, imageID digest와 AI gateway의 `/cicd-healthz` marker `ci-cd-probe`를 확인한다. bot 역할·승인·CSMS 호출과 Collector 데이터 흐름도 서비스 준비 후 확인한다.

## 검증 범위

`kubectl kustomize chargeops-chatops/overlays/dev`와 `scripts/validate-registry.sh`로 리소스 렌더링·Registry 접두사를 확인한다. 중복 리소스 ID, 이미지 항목, Image Updater 이미지·alias, Service selector·backend URL 및 PVC·ConfigMap 참조도 확인한다. 실제 이미지 존재 여부, Secret 값, 클러스터 rollout과 데이터 이전은 로컬 검증에 포함하지 않는다.
