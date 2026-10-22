# 토큰 및 필수 Secret을 사용하는 서비스 임시 중지

토큰 준비 전 실행을 보류했던 서비스 중 `chatops-bot`과 `alert-router`는 실행 설정으로 복원했습니다.
현재 중지 대상은 `notifier`, `csms` 2개입니다. bot 이름 충돌은 기존 `chatops-bot`을 우선하여 병합했습니다.
변경 위치는 모두 `chargeops-chatops/base/<서비스>/deployment.yaml`입니다.

현재 `chatops-bot`은 Socket Mode 수신 프로세스를 하나만 운영하도록 `replicas: 1`로 설정합니다.
업데이트 전략은 기존 `Recreate`를 유지합니다. 운영환경에서 수동 scale 대신 GitOps 설정으로 반영합니다.
Argo CD Sync 후 다음 명령의 출력이 `1 / Recreate`인지 확인합니다.

```bash
kubectl -n ops get deploy chatops-bot \
  -o jsonpath='{.spec.replicas}{" / "}{.spec.strategy.type}{"\n"}'
```

| 서비스 | 사용하는 토큰 / 필수 Secret |
|---|---|
| chatops-bot | SLACK_BOT_TOKEN, SLACK_APP_TOKEN, DB_DSN, CSMS_API_TOKEN, JENKINS_TOKEN / 기존 slack-credentials·bot-backend-credentials 및 chargeops-app-secrets |
| notifier | SLACK_BOT_TOKEN, CSMS_API_TOKEN / chargeops-app-secrets |
| csms | API_TOKEN 및 필수 DB_DSN / chargeops-app-secrets |

CSMS의 API_TOKEN은 코드에서 선택 사항이지만 Deployment가 필수 Secret을 참조하고 DB_DSN도 필요하므로
이번 준비 전 중지 대상에 포함했습니다. 실제 토큰 값은 파일에 추가하지 않았습니다.

Collector는 Deployment가 아닌 CronJob이며 EV_SERVICE_KEY와 DB 인증 정보가 필요합니다.
`collector-status`, `collector-info`, `collector-check`는 이미 모두 `suspend: true`이므로 그대로 유지합니다.

토큰 없이 실행 가능한 `ai-gateway`, `vcharger`의 replicas는 1을 유지합니다.
AI gateway의 DB Secret 참조는 optional이며 업무 API 사용에는 실제 DB 연결이 필요합니다.
vcharger는 중지된 csms에 연결할 수 없어 OCPP 업무 흐름은 사용할 수 없습니다.

## 반영 및 복원

최초 중지 변경은 커밋 `a907d18`로 main에 Push했습니다. 이번 복원 변경도 main에 커밋·Push합니다.
클러스터에서 직접 적용하거나 실행 상태를 확인하지는 않았습니다.
Argo CD 자동 Sync 후 복원한 Deployment의 Pod가 실행되며, 해당 서비스의 Secret과 ConfigMap이 필요합니다.
base의 replicas 변경이므로 해당 base를 사용하는 다른 overlay에도 적용됩니다.

Secret 및 외부 서비스 준비 후 실행할 Deployment의 replicas를 1로 되돌리고 GitOps로 반영합니다.
Collector는 DB/API/스토리지 확인 후 필요한 CronJob만 suspend를 false로 변경합니다.

Image Updater는 앱 이미지 6개를 추적합니다. 실행 Pod가 없는 Deployment의 이미지가 Argo CD Application
상태에 나타나지 않으면 Image Updater가 해당 이미지를 생략할 수 있으므로, 중지 중에도 digest 추적을
계속해야 한다면 해당 alias의 forceUpdate 설정을 별도로 확인합니다.

replicas 0은 토큰 관련 Pod 오류를 피하기 위한 임시 조치이며, PVC Pending 등 다른 리소스의
Progressing 문제까지 해결하지는 않습니다.
