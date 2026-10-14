pipeline {
  agent { label 'chargeops-agent' }
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
          kubectl kustomize chargeops-chatops/overlays/dev > artifacts/chargeops-dev.yaml
          test -s artifacts/chargeops-dev.yaml
        '''
      }
    }
    stage('Server dry-run') {
      when { expression { params.KUBECONFIG_CREDENTIAL_ID?.trim() } }
      steps {
        withCredentials([file(credentialsId: params.KUBECONFIG_CREDENTIAL_ID.trim(), variable: 'KUBECONFIG')]) {
          sh '''
            set -eu
            kubectl apply --dry-run=server -f artifacts/chargeops-dev.yaml
            kubectl apply --dry-run=server -f chargeops-chatops/argocd/application.yaml
            kubectl apply --dry-run=server -f chargeops-chatops/argocd/image-updater.yaml
          '''
        }
      }
    }
  }
  post {
    success {
      archiveArtifacts artifacts: 'artifacts/chargeops-dev.yaml', fingerprint: true
      echo 'Manifest validation passed. Argo CD and Image Updater handle deployment.'
    }
  }
}
