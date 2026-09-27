---
title: "Compute log files"
project: Nova
release: "2025.2"
source_url: https://docs.openstack.org/nova/2025.2/admin/configuration/logs.html
source_last_updated: "2019-10-02"
retrieved_at: "2026-09-27"
license: "CC BY 3.0"
---

> Source: [OpenStack Nova documentation](https://docs.openstack.org/nova/2025.2/admin/configuration/logs.html). Licensed under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/).

# Compute log files

The corresponding log file of each Compute service is stored in the
`/var/log/nova/` directory of the host on which each service runs.

Log files used by Compute services

| Log file | Service name (CentOS/Fedora/openSUSE/Red Hat Enterprise Linux/SUSE Linux Enterprise) | Service name (Ubuntu/Debian) |
| --- | --- | --- |
| `nova-api.log` | `openstack-nova-api` | `nova-api` |
| `nova-compute.log` | `openstack-nova-compute` | `nova-compute` |
| `nova-conductor.log` | `openstack-nova-conductor` | `nova-conductor` |
| `nova-manage.log` | `nova-manage` | `nova-manage` |
| `nova-scheduler.log` | `openstack-nova-scheduler` | `nova-scheduler` |
