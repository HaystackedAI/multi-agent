PY ?= .venv/bin/python
PORT ?= 8000

.PHONY: venv serve lint

venv:
	uv venv -p 3.12 .venv
	uv pip install --python .venv/bin/python -e ".[dev]"

# Requires AGENT_RUNTIME_ARN (the deployed recongraph runtime) and AWS credentials.
serve:
	$(PY) -m uvicorn app.server:app --reload --port $(PORT)

lint:
	$(PY) -m ruff check .
