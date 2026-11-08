.PHONY: setup up down logs test run status approve report design translation clean
setup:
	@test -f .env || (cp .env.example .env && echo "Created .env (dev-only credentials)")
up: setup
	docker compose --profile demo up -d --build
	@echo "Orchestrator: http://localhost:8000/docs   Neo4j: http://localhost:7474   MinIO: http://localhost:9001"
down:
	docker compose --profile demo --profile mcp down
logs:
	docker compose logs -f orchestrator
test:
	PYTHONPATH=src:tests:. python -m pytest -q tests
run:
	@curl -s -X POST localhost:8000/runs
status:
	@curl -s localhost:8000/runs/$(RUN) | python -m json.tool
report:
	@curl -s localhost:8000/runs/$(RUN)/assessment
design:
	@curl -s localhost:8000/runs/$(RUN)/report/design.json
translation:
	@curl -s localhost:8000/runs/$(RUN)/report/translation_report.md
approve:
	@curl -s -X POST localhost:8000/runs/$(RUN)/approve -H 'content-type: application/json' \
	  -d '{"approved": true, "approver": "$(USER)", "comment": "scope ok"}'
clean:
	docker compose --profile demo --profile mcp down -v
