# Foreman on Kubernetes

> **Experimental and unsupported.** This repository is an early community
> project. It does not provide a supported installation yet.

This project aims to deploy and operate Foreman with Katello on Kubernetes by
composing the existing Foreman, Candlepin, and Pulp containers. The applications
remain independently developed and released by their upstream projects. This
repository owns only the Kubernetes deployment, lifecycle orchestration, and
qualification needed to run them together as a compatible system.

The goal is to make containerized deployments reproducible and scalable without
forking the applications or requiring them to choose between Kubernetes and a
traditional installation. Reusable runtime capabilities and fixes belong in the
upstream application or image repository; Kubernetes-specific policy belongs
here.

## Project boundaries

- Foreman and Katello run together because Katello is a Foreman plugin.
- Candlepin remains a separate Java service.
- Pulp remains a family of API, content, and worker processes.
- Smart Proxies remain independent services for infrastructure-facing features
  such as DHCP, DNS, TFTP, Remote Execution, and Ansible.
- Databases, object storage, caches, ingress, secrets, backup, and recovery are
  explicit deployment dependencies rather than hidden application components.

The project does not replace Foreman application development, publish modified
application forks, or move infrastructure-facing Smart Proxy features into the
Foreman web workload.

## Development checks

Helm is required to lint the charts. Install it using the
[official Helm installation guide](https://helm.sh/docs/intro/install/) and
ensure `helm` is available on your `PATH`.

Run chart linting from the repository root with:

```console
make lint
```

This runs `helm lint` on each chart in the repository. CI's **Chart Lint** job
runs the same `make lint` command.

The first runnable milestone installs core Foreman with a disposable PostgreSQL
database. Integration tests require a native Linux AMD64 host, Podman, Kind,
kubectl, Helm 4, and Python 3.10 or newer. See the
[minimal Foreman quick start](docs/quick-start.md) for setup, test, and cleanup
commands. CI's **Foreman Installation** job uses those same Makefile targets and
publishes pytest results as JUnit XML.

## Status and collaboration

The repository is being established as a sequence of small, reviewable changes.
Architecture, testing, packaging, deployment, and lifecycle behavior will be
documented and implemented incrementally; this README describes only the stable
project boundary.

The work is coordinated in the
[Foreman on Kubernetes project](https://github.com/orgs/theforeman/projects/30).
Design questions can be discussed in a GitHub issue or in the
[Foreman community Development category](https://community.theforeman.org/c/development/9).
