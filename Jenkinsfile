pipeline {
  agent { label 'mgmt-agent' }
  options {
    timestamps()
    disableConcurrentBuilds()
    skipDefaultCheckout(true)
    timeout(time: 10, unit: 'MINUTES')
  }
  triggers { pollSCM('H/5 * * * *') }
  parameters {
    string(name: 'KUBECTL_VERSION', defaultValue: 'v1.36.1',
      description: 'Version downloaded when kubectl is missing. For server dry-run, match the cluster minor version.')
    string(name: 'KUBECONFIG_CREDENTIAL_ID', defaultValue: '',
      description: 'Optional Jenkins Secret file credential for server dry-run. Empty: render validation only.')
  }
  stages {
    stage('Checkout') {
      steps { checkout scm }
    }
    stage('Prepare kubectl') {
      steps {
        script {
          env.PATH = env.WORKSPACE + '/.ci-bin:' + env.PATH
        }
        withEnv(['KUBECTL_VERSION=' + params.KUBECTL_VERSION.trim()]) {
          sh 'sh scripts/ensure-kubectl.sh'
        }
      }
    }
    stage('Render manifests') {
      steps {
        sh '''
          set -eu
          mkdir -p artifacts
          python3.12 -m venv .ci-venv
          .ci-venv/bin/python -m pip install -r requirements-dev.txt
          .ci-venv/bin/python -m pytest -q tests/
          for ENVIRONMENT in dev development prod; do
            kubectl kustomize "chargeops-chatops/overlays/$ENVIRONMENT" > "artifacts/chargeops-$ENVIRONMENT.yaml"
            test -s "artifacts/chargeops-$ENVIRONMENT.yaml"
            sh scripts/validate-registry.sh "artifacts/chargeops-$ENVIRONMENT.yaml"
          done
        '''
      }
    }
    stage('Server dry-run') {
      when { expression { params.KUBECONFIG_CREDENTIAL_ID?.trim() } }
      steps {
        withCredentials([file(credentialsId: params.KUBECONFIG_CREDENTIAL_ID.trim(), variable: 'KUBECONFIG')]) {
          sh '''
            set -eu
            kubectl apply --dry-run=server -f artifacts/chargeops-development.yaml
            kubectl apply --dry-run=server -f artifacts/chargeops-prod.yaml
            kubectl apply --dry-run=server -f chargeops-chatops/argocd/application.yaml
            kubectl apply --dry-run=server -f chargeops-chatops/argocd/application-dev.yaml
            kubectl apply --dry-run=server -f chargeops-chatops/argocd/dev-verifier-rbac.yaml
            kubectl apply --dry-run=server -f chargeops-chatops/argocd/image-updater.yaml
          '''
        }
      }
    }
  }
  post {
    success {
      archiveArtifacts artifacts: 'artifacts/chargeops-*.yaml', fingerprint: true
      echo 'Manifest validation passed. Argo CD and Image Updater handle deployment.'
    }
    cleanup {
      sh 'rm -rf .ci-venv'
    }
  }
}
