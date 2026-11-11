# Component limits

This page lists the workloads of the `foreman` and `foreman-proxy` Helm charts
and the services they depend on, with the number of replicas each can run, the
state it keeps and its known limits. The project is experimental: a replica
count in this table is what the chart allows, not a supported production
configuration. The linked sections of the [quick start](quick-start.md)
explain each component, and
[Replicas and disruptions](quick-start.md#replicas-and-disruptions) explains
the PodDisruptionBudgets (PDBs) and how replicas are spread across nodes.

A workload that always runs one replica is unavailable when its node is
drained or lost, until Kubernetes starts a replacement pod.

## `foreman` Helm chart

| Component | Replicas | State | Limits |
| --- | --- | --- | --- |
| Foreman web | `replicaCount` | None in the pod. Sessions are stored in PostgreSQL, the Rails cache in Valkey when Dynflow is enabled | With Katello, every web pod runs its own Katello event daemon; whether several daemons handle an event twice has not been tested. [katello#11904](https://github.com/Katello/katello/pull/11904) and [katello#11948](https://github.com/Katello/katello/pull/11948) are two open proposals to replace the daemon with Foreman tasks. See also [Enable Katello](quick-start.md#enable-katello) |
| httpd | `httpd.replicas` | None | None known |
| Dynflow orchestrator | always 1 | None in the pod; tasks are stored in PostgreSQL and queued in Valkey | No new task starts until the replacement is ready. See [Run background tasks](quick-start.md#run-background-tasks) |
| Dynflow workers | `dynflow.worker.replicas`, `dynflow.hostsQueue.replicas` | None in the pod | Sidekiq waits up to 25 seconds for running jobs before a pod stops |
| Cron tasks | One CronJob per period, at most one run of a period at a time | None in the pod | A run that fails or is interrupted is not retried; the next scheduled run repeats the tasks. See [Run periodic tasks](quick-start.md#run-periodic-tasks) |
| Candlepin | always 1 | Data of its embedded message broker, lost when the pod is replaced | Requests that need Candlepin wait or fail until the replacement is ready, 32 to 53 seconds in the Kind disruption tests. The migration Job runs the Liquibase command that [candlepin-oci-images#90](https://github.com/theforeman/candlepin-oci-images/pull/90) would provide in the image. See [Enable Candlepin](quick-start.md#enable-candlepin) |
| Smart Proxy (`proxy.enabled`) | always 1 | Running Remote Execution jobs, in memory | No job runs until the replacement is ready. See [Run remote execution jobs](quick-start.md#run-remote-execution-jobs) |
| Pulp API, content app, worker | `pulp.api.replicas`, `pulp.content.replicas`, `pulp.worker.replicas` | Content on a PersistentVolumeClaim, by default ReadWriteOnce, or in an S3 bucket | On colocated filesystem storage, the default, all Pulp pods run on one node and get no PDB and no spreading. Content and the Pulp API are then unavailable until a pod can mount the volume again; with storage that is local to the node, until the node is back. The published Pulp image lacks the S3 libraries ([pulp-oci-images#117](https://github.com/theforeman/pulp-oci-images/pull/117)). See [Enable Pulp](quick-start.md#enable-pulp) |

## `foreman-proxy` Helm chart

| Component | Replicas | State | Limits |
| --- | --- | --- | --- |
| httpd | `httpd.replicas` | None | None known. With more than one replica, a PDB and spreading as in the `foreman` chart |
| foreman-proxy | always 1 | Running Remote Execution and Ansible jobs, in memory. Container gateway data in PostgreSQL | See [Remote Execution and Ansible](quick-start.md#remote-execution-and-ansible) |
| Pulp API, content app, worker | `pulp.api.replicas`, `pulp.content.replicas`, `pulp.worker.replicas` | As in the `foreman` chart | As in the `foreman` chart |

`tools/backup.sh` and `tools/restore.sh` also cover a `foreman-proxy` release. See
[Back up a Smart Proxy with content](quick-start.md#back-up-a-smart-proxy-with-content).

## Services the charts do not install

| Service | Used by | State | Limits |
| --- | --- | --- | --- |
| PostgreSQL | Foreman, Candlepin, Pulp, the container gateway | Every database | Tested with PostgreSQL 16 in a single pod. `tools/backup.sh` dumps the databases of both charts, including the container gateway database. Availability and replication are up to the database operator |
| Valkey or Redis | Dynflow, the Rails cache, Pulp | Task queue and caches | Not backed up. Tested only without Transport Layer Security (TLS); a private certificate authority for Dynflow's Redis connection needs [foreman#11304](https://github.com/theforeman/foreman/pull/11304) |
| S3-compatible object store | Pulp with `pulp.storage.type=s3` | Pulp content | Tested with MinIO. Not backed up by `tools/backup.sh` |
