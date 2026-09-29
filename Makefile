.PHONY: help dev prod test lint deploy teardown

help:
	@echo "make dev       - local stack with hot reload (docker compose)"
	@echo "make prod      - run the production image locally"
	@echo "make test      - run unit tests"
	@echo "make lint      - ruff"
	@echo "make deploy    - deploy function + Cloud Run (needs PROJECT_ID)"
	@echo "make teardown  - delete all GCP resources (needs PROJECT_ID)"

dev:
	docker compose up --build

prod:
	docker compose -f docker-compose.prod.yml up --build

test:
	pytest -v

lint:
	ruff check .

deploy:
	./deployment/deploy.sh

teardown:
	./deployment/teardown.sh
