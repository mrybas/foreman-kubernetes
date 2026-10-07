# Foreman on Kubernetes

## Build and test

- Lint: `make lint`
- Integration: `make setup-test-env`, `make setup-kind`, `make test`
- Diagnostics: `make diagnostics`; cleanup: `make teardown-kind`
- Integration uses native Linux AMD64 and defaults to Podman; see
  `docs/quick-start.md` for dependencies and privileges.

## Project pointers

- `README.md`: project scope, boundaries, and status. The project is experimental
  and unsupported.
- `CONTRIBUTING.md` and `.github/PULL_REQUEST_TEMPLATE.md`: contribution and
  pull request guidance.
- `docs/roadmap.md`: intended high-level sequence, not a live task tracker; use
  the linked issues and project board for current status.
