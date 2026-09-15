PY ?= .venv/bin/python
.PHONY: test lint identity-doc
test:
	$(PY) -m pytest
lint:
	.venv/bin/ruff check src tests scripts && .venv/bin/ruff format --check src tests scripts && $(PY) -m mypy src
identity-doc:
	$(PY) scripts/gen-identity-doc.py identity/role-requirements.yaml docs/identity-requirements.md
