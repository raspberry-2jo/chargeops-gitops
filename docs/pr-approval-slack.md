# 앱 PR 승인 / Slack 연결

새 흐름은 앱 저장소의 kim_chanju → dev PR 승인·Merge를 운영 승격 기준으로 사용한다. GitOps main은 기존처럼 Jenkins가 Dev/Ops Digest를 직접 갱신하며, 실패 시 이전 Digest 자동 복구를 유지한다. 자동 CD 대상은 CSMS다.

전체 Jenkins Job·Credential·GitHub 보호 규칙·검증 절차는 [앱 저장소 설정 문서](https://github.com/raspberry-2jo/chargeops/blob/kim_chanju/docs/pr-approval-slack.md)를 따른다.

실서버의 Dev Application은 chargeops-dev이고 Ops Application은 chargeops-chatops-dev다. 이름에 dev가 붙어 있어도 namespace가 ops이면 운영용이다. Dev 계정에 운영 조회 권한을 추가하지 않고 Jenkins가 올바른 Application을 사용하게 한다.

기존 서버 Application 이름에 제한된 Jenkins 권한을 연결하는 명령:

```bash
.ci-venv/bin/python scripts/configure-ci-app-access.py \
  --dev-application chargeops-dev \
  --ops-application chargeops-chatops-dev
```

이름은 실제 서버의 Application을 지정한다. Dev는 namespace dev / overlays/development, Ops는 namespace ops / overlays/canary / Manual Sync여야 한다. 스크립트는 이 조건을 확인한 후 argocd namespace의 기존 Role/RoleBinding 네 개만 적용한다. 기존 토큰을 재사용하며 Secret·Application·워크로드를 변경하지 않는다. 이 연결을 위해 bootstrap-canary.py를 다시 실행하거나 Application을 재생성하지 않는다.

Git에 남아 있는 예전 Application 이름과 실서버 이름이 다를 수 있다. 다음 출력으로 실제 소유 관계를 확인하고 Jenkins 파라미터와 위 명령에 동일하게 지정한다. 기존 리소스에 다른 Application 소유자를 추가하지 않는다.

```bash
kubectl -n argocd get applications \
  -o custom-columns=NAME:.metadata.name,NAMESPACE:.spec.destination.namespace,PATH:.spec.source.path
```

Image Updater는 계속 중지한다. Ops Auto Sync도 켜지 않는다. 승인된 dev Job이 선택 Sync를 요청한다. GitOps 저장소 main의 쓰기 권한을 차단하면 자동 복구도 차단되므로, 앱 dev의 PR 보호 규칙과 구분해서 관리한다.
