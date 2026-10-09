PY ?= .venv/bin/python
.PHONY: test lint identity-doc image vm-install vm-package insights-package
test:
	$(PY) -m pytest
lint:
	.venv/bin/ruff check src tests scripts ansible && .venv/bin/ruff format --check src tests scripts ansible && $(PY) -m mypy src
identity-doc:
	$(PY) scripts/gen-identity-doc.py identity/role-requirements.yaml docs/identity-requirements.md
# Manual build + push (see docs/agents/deployment.md). e.g. make image PARAMS=deploy.env ARGS=--deploy
image:
	scripts/build-image.sh $(if $(PARAMS),--param-file $(PARAMS)) $(if $(ACR),--acr-name $(ACR)) $(ARGS)
# Install/update on a RHEL 9 VM (Path C). e.g. make vm-install PARAMS=vm.env ARGS='--release-ref <sha>'
vm-install:
	scripts/vm-install.sh $(if $(PARAMS),--param-file $(PARAMS)) $(ARGS)
# Self-contained RHEL 9 VM package: no git/GitHub/GitLab on the VM (docs/ops/azure-vm.md). e.g. make vm-package VERSION=7
vm-package:
	scripts/build-vm-package.sh --version $(VERSION) $(ARGS)
# Azure half of a release: insights Terraform + install.sh (docs/ops/insights.md). e.g. make insights-package VERSION=7
insights-package:
	scripts/build-insights-package.sh --version $(VERSION) $(ARGS)
