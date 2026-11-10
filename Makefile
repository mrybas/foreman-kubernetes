TEST_PYTHON ?= .venv/bin/python
KIND_CLUSTER_NAME ?= foreman-minimal
KIND_NAMESPACE ?= foreman
HELM_RELEASE ?= foreman
KUBECONFIG := $(CURDIR)/artifacts/kubeconfig
export KIND_CLUSTER_NAME KIND_NAMESPACE HELM_RELEASE KUBECONFIG
export KIND_EXPERIMENTAL_PROVIDER ?= podman
# Foreman images for test-upgrade. It installs UPGRADE_FROM_IMAGE_DIGEST, the
# 5.0.1 release, and upgrades to the image that UPGRADE_TO_IMAGE_TAG points to
# when the tests start, or to UPGRADE_TO_IMAGE_DIGEST if set (to repeat a run).
# quay.io/foreman/foreman keeps release tags but not old nightly digests. After
# a release, set the first to the digest of its tag:
#   skopeo inspect --format '{{.Digest}}' docker://quay.io/foreman/foreman:5.0.1
UPGRADE_FROM_IMAGE_DIGEST ?= sha256:940408adc6cdf0c2996e61d0880095f10f14813be1bf54cde340320238c4e722
UPGRADE_TO_IMAGE_TAG ?= nightly
UPGRADE_TO_IMAGE_DIGEST ?=
export UPGRADE_FROM_IMAGE_DIGEST UPGRADE_TO_IMAGE_TAG UPGRADE_TO_IMAGE_DIGEST

.PHONY: lint setup-test-env test-static setup-kind test test-backup test-upgrade teardown-kind diagnostics
lint:
	@find . -type f -name Chart.yaml -not -path './.git/*' -exec sh -c 'for chart do helm lint "$${chart%/Chart.yaml}" || exit $$?; done' sh {} +

setup-test-env:
	python3 -m venv .venv
	$(TEST_PYTHON) -m pip install -r tests/requirements.txt

test-static:
	$(TEST_PYTHON) -m pytest -v tests/static --junitxml=artifacts/junit-static.xml

setup-kind:
	tests/kind/run-minimal.sh setup

test:
	tests/kind/run-minimal.sh deploy
	$(TEST_PYTHON) -m pytest -v tests/integration -m "not backup" --junitxml=artifacts/junit.xml

# Backs up the release installed by test, deletes its namespace and restores
# it into $(KIND_NAMESPACE)-restore. Run it after test.
test-backup:
	$(TEST_PYTHON) -m pytest -v tests/integration -m backup --junitxml=artifacts/junit-backup.xml

test-upgrade:
	tests/kind/run-minimal.sh deploy --set-string image.digest=$(UPGRADE_FROM_IMAGE_DIGEST)
	$(TEST_PYTHON) -m pytest -v tests/upgrade tests/integration -m "not backup" --junitxml=artifacts/junit.xml

diagnostics:
	tests/kind/run-minimal.sh diagnostics

teardown-kind:
	tests/kind/run-minimal.sh teardown
