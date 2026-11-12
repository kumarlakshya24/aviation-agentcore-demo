# Halcyon Flight Ops. install/run-local/test/lint/capture are offline; deploy/destroy touch AWS.
.PHONY: install run-local run-live test lint format capture infra deploy destroy

install:
	uv sync

# Offline mode: synthetic fixtures, no AWS calls, loopback only.
run-local:
	DEMO_MODE=offline HOST=127.0.0.1 uv run python app.py

# Live mode: needs .env (AGENT_RUNTIME_ARN etc.) and AWS credentials.
run-live:
	set -a; . ./.env; set +a; DEMO_MODE=live uv run python app.py

test:
	uv run pytest -q

lint:
	uvx ruff check .
	uvx ruff format --check .

format:
	uvx ruff format .
	uvx ruff check --fix .

# Screenshots for docs/assets (starts the offline app on 127.0.0.1, stops it after).
capture:
	uv run --with playwright python scripts/capture_screenshots.py

# Creates billable resources in the account your AWS credentials point at.
infra:
	./scripts/setup_demo_infra.sh

deploy:
	./scripts/deploy_mcp.sh

destroy:
	./scripts/destroy.sh
