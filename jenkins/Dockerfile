# Local Jenkins for this repo: plugins preinstalled, no setup wizard, and the
# pipeline job created on boot by Configuration-as-Code (casc.yaml).
FROM jenkins/jenkins:lts-jdk17

# Local demo only: stays root so builds can use the host's Docker socket without
# matching group IDs across Docker Desktop / Linux hosts. Don't run a shared Jenkins like this.
USER root

# Docker CLI only; builds run on the host daemon through the mounted socket.
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.3.1.tgz \
      | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

COPY casc.yaml /casc/casc.yaml
ENV CASC_JENKINS_CONFIG=/casc/casc.yaml
