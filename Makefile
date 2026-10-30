.PHONY: help dev prod monitoring jenkins traffic test lint deploy teardown

help:
	@echo "make dev       - local stack with hot reload (docker compose)"
	@echo "make prod      - run the production image locally"
	@echo "make monitoring - dev stack + Prometheus (:9090) + Grafana (:3000)"
	@echo "make jenkins   - local Jenkins (:8081) running ./Jenkinsfile"
	@echo "make traffic   - send demo traffic to localhost:8080"
	@echo "make test      - run unit tests"
	@echo "make lint      - ruff"
	@echo "make deploy    - deploy function + Cloud Run (needs PROJECT_ID)"
	@echo "make teardown  - delete all GCP resources (needs PROJECT_ID)"

dev:
	docker compose up --build

prod:
	docker compose -f docker-compose.prod.yml up --build

monitoring:
	docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up --build

jenkins:
	docker compose -f docker-compose.jenkins.yml up -d --build

traffic:
	python scripts/generate_traffic.py --minutes 3

test:
	pytest -v

lint:
	ruff check .

deploy:
	./deployment/deploy.sh

teardown:
	./deployment/teardown.sh
