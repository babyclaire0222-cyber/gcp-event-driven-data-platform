// Same checks as .github/workflows/ci.yml, plus a gated deploy stage.
// Runs on the local Jenkins in jenkins/ (docker compose -f docker-compose.jenkins.yml up)
// or any Jenkins with the Docker Pipeline plugin and access to a Docker daemon.
pipeline {
    agent none

    options {
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
        disableConcurrentBuilds()
    }

    parameters {
        booleanParam(name: 'DEPLOY', defaultValue: false,
                     description: 'After tests pass, deploy to GCP (asks for approval first)')
    }

    environment {
        IMAGE = "gcp-data-platform-webapp:${env.BUILD_NUMBER}"
    }

    stages {
        stage('Lint & unit tests') {
            agent { docker { image 'python:3.12-slim'; args '-u root' } }
            steps {
                sh 'pip install --no-cache-dir -q -r requirements-dev.txt'
                sh 'ruff check .'
                sh 'pytest --junitxml=reports/junit.xml'
                sh 'for f in deployment/*.sh; do bash -n "$f"; done'
                sh 'python -m json.tool monitoring/grafana/dashboards/webapp.json > /dev/null'
            }
            post {
                always { junit 'reports/junit.xml' }
            }
        }

        stage('Validate alert rules') {
            agent { docker { image 'prom/prometheus:v2.54.1'; args '--entrypoint=' } }
            steps {
                sh 'promtool check rules monitoring/prometheus/alerts.yml'
            }
        }

        stage('Build image') {
            agent any
            steps {
                sh 'docker build -t "$IMAGE" ./webapp'
            }
        }

        stage('Smoke test') {
            agent any
            steps {
                sh '''
                    docker run -d --name "smoke-$BUILD_NUMBER" -e GCP_PROJECT=ci -e BUCKET_NAME=ci "$IMAGE"
                    for i in $(seq 1 20); do
                        if docker exec "smoke-$BUILD_NUMBER" python -c "
import urllib.request as u
assert u.urlopen('http://localhost:8080/healthz').status == 200
assert b'webapp_http_requests_total' in u.urlopen('http://localhost:8080/metrics').read()
"; then echo "smoke test passed"; exit 0; fi
                        sleep 1
                    done
                    docker logs "smoke-$BUILD_NUMBER"; exit 1
                '''
            }
            post {
                always { sh 'docker rm -f "smoke-$BUILD_NUMBER" || true' }
            }
        }

        stage('Deploy to GCP') {
            when { expression { params.DEPLOY } }
            // Asked before an agent is allocated, so nothing sits idle waiting for a human.
            input {
                message 'Deploy the Cloud Function and Cloud Run service to GCP?'
                ok 'Deploy'
            }
            agent { docker { image 'google/cloud-sdk:alpine'; args '-u root' } }
            steps {
                withCredentials([
                    file(credentialsId: 'gcp-sa-key', variable: 'GOOGLE_APPLICATION_CREDENTIALS'),
                    string(credentialsId: 'gcp-project-id', variable: 'PROJECT_ID'),
                ]) {
                    sh '''
                        gcloud components install beta --quiet
                        gcloud auth activate-service-account --key-file="$GOOGLE_APPLICATION_CREDENTIALS"
                        ./deployment/deploy.sh
                    '''
                }
            }
        }
    }

    post {
        always {
            node('built-in') {
                sh 'docker image rm "$IMAGE" || true'
            }
        }
    }
}
