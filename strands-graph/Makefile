PY ?= .venv/bin/python
PORT ?= 8000

.PHONY: venv seed serve sweep test lint deploy demo

venv:
	uv venv -p 3.12 .venv
	uv pip install --python .venv/bin/python -e ".[dev]"

seed:
	$(PY) -m chaser.cli seed

serve:
	$(PY) -m uvicorn app.server:app --reload --port $(PORT)

sweep:
	$(PY) -m chaser.cli sweep

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

deploy:
	./scripts/deploy_agentcore.sh

deploy-web:
	./scripts/deploy_web.sh

demo:
	./scripts/demo.sh
