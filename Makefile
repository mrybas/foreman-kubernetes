TEST_PYTHON ?= .venv/bin/python
KIND_CLUSTER_NAME ?= foreman-minimal
KIND_NAMESPACE ?= foreman
HELM_RELEASE ?= foreman
KUBECONFIG := $(CURDIR)/artifacts/kubeconfig
export KIND_CLUSTER_NAME KIND_NAMESPACE HELM_RELEASE KUBECONFIG
export KIND_EXPERIMENTAL_PROVIDER ?= podman

.PHONY: lint setup-test-env setup-kind test teardown-kind diagnostics
lint:
	@find . -type f -name Chart.yaml -not -path './.git/*' -exec sh -c 'for chart do helm lint "$${chart%/Chart.yaml}" || exit $$?; done' sh {} +

setup-test-env:
	python3 -m venv .venv
	$(TEST_PYTHON) -m pip install -r tests/requirements.txt

setup-kind:
	tests/kind/run-minimal.sh setup

test:
	tests/kind/run-minimal.sh deploy
	$(TEST_PYTHON) -m pytest -v tests/integration --junitxml=artifacts/junit.xml

diagnostics:
	tests/kind/run-minimal.sh diagnostics

teardown-kind:
	tests/kind/run-minimal.sh teardown
