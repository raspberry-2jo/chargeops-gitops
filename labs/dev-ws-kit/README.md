# 첨부 Lab Kit에서 가져온 도구

실제 Dev 설치·실행은 ../../docs/dev-ws-lab-20261031/guide.html을 먼저 읽는다. 기존 예제 YAML과 이전 HTML은 적용 대상으로 가져오지 않았다. HPA는 이번 변경 범위에 포함하지 않았다.

원본 ZIP의 출처 해시는 SOURCE.json에 있다. scripts/, tests/, results/를 보존했다. 변경점은 observe_rollout.py에 명시적 --context 필수 추가, ocpp_load.py에 CI-DEV-WSLAB- ID prefix의800개 상한 추가, tuning CSV의 minReadySeconds30초다.

`python -m pytest -q labs/dev-ws-kit/tests`는 산술·자료 해석의 합성 테스트다. 실제 CSMS 부하 시험 통과 증거가 아니다. 내장2개 cohort와 외부 클라이언트에서 같은 ID를 동시에 사용하지 않는다.
