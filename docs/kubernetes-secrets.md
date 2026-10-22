# Kubernetes Secret 준비 목록

현재 렌더링되는 GitOps 매니페스트 기준으로 직접 참조하는 Secret은 총 3개입니다.
앱용 2개는 `ops`, Image Updater Git write-back용 1개는 `argocd`에서 준비합니다.
이 저장소는 Secret 생성 매니페스트나 실제 값을 포함하지 않습니다.
실제 클러스터의 생성 여부와 값은 확인하지 않았습니다.

## 앱용 Secret: ops namespace

| Secret 이름 | 준비할 키 | 사용 리소스 |
|---|---|---|
| `chargeops-secrets` | `DB_DSN`, `CSMS_API_TOKEN`, Slack 토큰·채널 등 아래 앱별 키 | csms, chatops-bot, notifier |
| `chargeops-app-secrets` | 아래 공통 키 목록 중 각 앱이 사용하는 키 | ai-gateway, Collector CronJob 3개 |

CSMS는 사용자가 존재와 키 목록을 확인한 기존 `ops/chargeops-secrets`를 사용한다.
`DB_DSN`은 같은 이름의 환경변수로, `CSMS_API_TOKEN`은 CSMS가 읽는 `API_TOKEN` 환경변수로 주입한다.
두 키만 `secretKeyRef`로 참조하며 실제 값과 DB 연결 성공 여부는 확인하지 않았다.
bot/notifier가 사용하는 `CSMS_API_TOKEN` 값은 CSMS에 주입되는 토큰과 일치해야 한다.

bot과 notifier는 `chargeops-secrets`를 `envFrom`으로 주입합니다.
사용자가 제공한 키 목록에는 notifier가 필수로 읽는 `SLACK_ALERT_CHANNEL`이 없으므로 실제 알림 채널 ID를 추가해야 합니다.
bot의 `SLACK_AUDIT_CHANNEL`은 선택 항목이며 감사 알림을 사용할 경우 추가합니다.
bot의 역할·승인 정책은 DB의 `ops_role` 등을 사용하므로 DB_DSN도 준비해야 합니다.
이전 slack-credentials·bot-backend-credentials와 제외된 router용 Secret은 현재 렌더링되는 앱의 필수 목록에서 제외했습니다. 클러스터에 있는 Secret 자체를 삭제하지는 않았습니다.

### 앱 Secret 키 목록

| 키 | 용도 / 값 준비 |
|---|---|
| `DB_DSN` | CSMS·bot·notifier·AI gateway의 PostgreSQL/PgBouncer 접속 문자열. DB 주소·포트·DB명·사용자·비밀번호 포함 |
| `API_TOKEN` | CSMS가 받는 내부 API 인증 토큰 |
| `CSMS_API_TOKEN` | bot/notifier가 CSMS에 보내는 토큰. 인증을 켜면 `API_TOKEN`과 동일 값 |
| `SLACK_BOT_TOKEN` | Slack Bot OAuth 토큰 (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Slack Socket Mode App 토큰 (`xapp-...`) |
| `SLACK_ALERT_CHANNEL` | 알림 대상 Slack 채널 ID |
| `SLACK_AUDIT_CHANNEL` | 감사 알림 대상 Slack 채널 ID |
| `JENKINS_USER` | bot에서 Jenkins API를 호출하는 사용자 |
| `JENKINS_TOKEN` | 해당 사용자의 Jenkins API 토큰 |
| `EV_SERVICE_KEY` | Collector 공공데이터 API 인증키 |
| `PGPASSWORD` | Collector DB 접속 비밀번호. 기존 ConfigMap의 `PGUSER: app_writer` 계정과 일치 |

공통 키 목록은 기존 GitOps 실행 문서의 준비 목록입니다.
로컬 CSMS 소스에서 `DB_DSN` 필수 및 `API_TOKEN` 선택 사용을 확인했으며,
로컬 Collector 소스에서 `EV_SERVICE_KEY`(또는 `PUBLIC_API_KEY`), `PGPASSWORD`(또는 `DB_PASSWORD`) 지원을 확인했습니다.
앱 저장소 루트 `apps/`의 bot/notifier/AI gateway 소스와 기존 문서를 함께 확인했습니다.
Registry에 올라간 실제 이미지 내용과 클러스터의 Secret 값은 확인하지 않았습니다.
S3 업로드를 켜는 경우 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` 및 필요 시 `AWS_SESSION_TOKEN` 등
해당 실행 환경의 인증 방식에 맞는 추가 설정이 필요합니다. 현재 Collector의 `S3_BUCKET`은 비어 있습니다.

### 필수 여부와 현재 실행 상태

- `chatops-bot`, `csms`, `notifier`는 replicas 2이며 `chargeops-secrets`를 사용합니다. CSMS는 위 두 키, bot은 `DB_DSN`·`SLACK_BOT_TOKEN`·`SLACK_APP_TOKEN`, notifier는 `DB_DSN`·`SLACK_BOT_TOKEN`·`SLACK_ALERT_CHANNEL` 등이 필요합니다. bot의 Recreate 전략은 유지합니다.
- Collector CronJob 3개는 `suspend: true`이며, 실행 또는 수동 Job 생성 전에 API/DB 키를 준비합니다.
- `ai-gateway`의 `DB_DSN` 참조만 `optional: true`입니다. Secret/키 없이 Pod를 시작할 수 있게 한 설정이며
  DB를 사용하는 업무 기능에 실제 연결이 필요하다는 기존 문서 조건은 별개입니다.
- `envFrom.secretRef`는 키별 목록을 선언하지 않습니다. Secret이 있어도 앱이 읽는 키가 빠지면 앱 실행·기능이 실패할 수 있습니다.

## GitOps용 Secret: argocd namespace

| Secret 이름 | 키 | 용도 |
|---|---|---|
| `chargeops-git-write` | `username`, `password` | Image Updater가 GitOps 저장소 main에 digest 커밋을 Push |

현재 저장소 URL은 HTTPS이며 `password`에는 GitHub 저장소 쓰기 권한이 있는 PAT를 사용합니다.
대상은 `raspberry-2jo/chargeops-gitops`이며 fine-grained PAT를 사용하면 해당 저장소의 Contents Read/Write 권한을 준비합니다.
branch protection 등 저장소 정책이 직접 Push를 허용하는지도 확인합니다.
현재 참조는 `git:secret:chargeops-git-write`이므로 `ops`가 아닌 ImageUpdater와 같은 `argocd`에 있어야 합니다.
이 Secret은 Docker Registry 로그인과 관계가 없습니다.
키 형식의 근거: [Image Updater Git 인증 공식 문서](https://argocd-image-updater.readthedocs.io/en/stable/basics/update-methods/#specifying-git-credentials).

저장소가 private이면 Argo CD에도 별도의 저장소 읽기 인증이 필요합니다.
기존에 등록된 repository/repo-creds 설정을 사용할 수 있으며,
`chargeops-git-write`를 만들었다고 Argo CD의 저장소 읽기 인증에 자동 등록되는 것은 아닙니다.
설치가 관리하는 Argo CD 내부 Secret은 위 앱 전용 준비 목록에 포함하지 않았습니다.

## 서로 맞춰야 할 값

- `INTERNAL_TOKEN`: bot-backend와 router Secret 및 실제 연동 대상 CSMS의 인증 설정을 맞춥니다.
- `ALLOWED_APPROVER_IDS`: bot-backend의 승인자 목록을 실제 운영 정책에 맞춥니다.
- 기존 bot과 router가 같은 Slack 앱을 쓰면 두 Secret의 `SLACK_BOT_TOKEN`을 동일하게 맞춥니다.
- `TRIGGER_KEY`: router Secret과 알림을 발생시키는 호출자가 같은 값을 사용합니다.
- `API_TOKEN`과 `CSMS_API_TOKEN`: CSMS  인증을 사용할 경우 동일하게 맞춥니다.
- `DB_DSN`과 `PGPASSWORD`: 각각 실제 접속 계정의 비밀번호와 맞춥니다. 두 앱이 같은 DB 계정을 쓰는 경우 그 계정 기준으로 일치시킵니다.

## 생성 및 조회 예시

실제 값은 저장소 밖 관리 VM의 권한 600 env 파일로 준비합니다.
각 파일에는 해당 Secret의 `KEY=value` 항목만 넣고 따옴표 없는 env 파일 형식으로 작성합니다.
다음 명령은 값이 채워진 파일을 준비한 뒤 관리 VM에서 실행합니다.

```bash
set -euo pipefail
kubectl create namespace ops --dry-run=client -o yaml | kubectl apply -f -
# ~/.chargeops-secrets 디렉터리 권한 700, 개별 파일 권한 600으로 사전 준비
for name in chargeops-app-secrets; do
  kubectl -n ops create secret generic "$name" \
    --from-env-file="$HOME/.chargeops-secrets/$name.env" \
    --dry-run=client -o yaml | kubectl apply -f -
done
# argocd namespace는 Argo CD 설치 시 준비되어 있어야 함
kubectl -n argocd create secret generic chargeops-git-write \
  --from-env-file="$HOME/.chargeops-secrets/chargeops-git-write.env" \
  --dry-run=client -o yaml | kubectl apply -f -

# 값 출력 없이 리소스 존재 확인
kubectl -n ops get secret chargeops-app-secrets
kubectl -n argocd get secret chargeops-git-write
```

기존 namespace에 Secret이 있어도 `ops`에서 자동 공유되지 않습니다.
Secret 값을 환경변수로 주입한 기존 Pod에는 갱신 값이 자동 반영되지 않으므로 필요 시 대상 워크로드를 재시작합니다.
중지해 둔 서비스는 준비가 끝나기 전까지 재개하지 않습니다.

## Secret이 아닌 추가 준비물

- `ops/chatops-config` ConfigMap은 통합한 bot이 더 이상 참조하지 않습니다. 기존 클러스터의 ConfigMap 자체는 삭제하지 않았습니다.
  bot의 CSMS_URL·AI_URL·JENKINS_URL은 Deployment에 명시합니다.
  bot은 새 이미지의 `chatops_bot.app`으로 실행하고 CSMS의 복구 API를 사용합니다.
- `ops/collector-config` ConfigMap은 이 저장소가 배포하며 `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` 등을 담습니다.
- Docker Registry는 인증 없이 사용하므로 `harbor-pull` 또는 다른 image pull Secret은 필요 없습니다.
- Jenkins의 SCM credential과 선택적 `KUBECONFIG_CREDENTIAL_ID`는 Jenkins에 등록하는 credential이며 앱용 Kubernetes Secret이 아닙니다.

## 조사 근거

- 이 저장소의 Deployment/CronJob의 `secretRef`/`secretKeyRef`와 ImageUpdater의 write-back 설정 전체
- `docs/chargeops-gitops.md`, `docs/token-services-paused.md`
- `C:/ChargeOps/chargeops/app/bot.py`, `app/router.py`
- `C:/ChargeOps/work/chargeops/apps/csms/app.py`, `apps/collector/collector_v1.py`

로컬 소스가 실제 레지스트리 이미지의 빌드 원본과 동일한 커밋인지는 확인하지 않았습니다.
현재 `ops` namespace를 사용하는 위 생성 명령을 기준으로 준비합니다.
