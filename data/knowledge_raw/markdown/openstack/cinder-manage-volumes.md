---
title: "Manage volumes"
project: Cinder
release: "2025.2"
source_url: https://docs.openstack.org/cinder/2025.2/admin/manage-volumes.html
source_last_updated: "2021-09-24"
retrieved_at: "2026-09-27"
license: "CC BY 3.0"
---

> Source: [OpenStack Cinder documentation](https://docs.openstack.org/cinder/2025.2/admin/manage-volumes.html). Licensed under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/).

# Manage volumes

The default OpenStack Block Storage service implementation is an
iSCSI solution that uses [Logical Volume Manager (LVM)](https://docs.openstack.org/cinder/2025.2/common/glossary.html#term-Logical-Volume-Manager-LVM) for Linux.

Note

The OpenStack Block Storage service also provides drivers that
enable you to use several vendors’ back-end storage devices in
addition to the base LVM implementation. These storage devices can
also be used instead of the base LVM installation.

This high-level procedure shows you how to create and attach a volume
to a server instance.

**To create and attach a volume to an instance**

1. Configure the OpenStack Compute and the OpenStack Block Storage
   services through the `/etc/cinder/cinder.conf` file.
2. Use the **openstack volume create** command to create a volume.
   This command creates an LV into the volume group (VG) `cinder-volumes`.
3. Use the **openstack server add volume** command to attach the
   volume to an instance. This command creates a unique [IQN](https://docs.openstack.org/cinder/2025.2/common/glossary.html#term-iSCSI-Qualified-Name-IQN) that is exposed to the compute node.

   - The compute node, which runs the instance, now has an active
     iSCSI session and new local storage (usually a `/dev/sdX`
     disk).
   - Libvirt uses that local storage as storage for the instance. The
     instance gets a new disk (usually a `/dev/vdX` disk).

For this particular walkthrough, one cloud controller runs
`nova-api`, `nova-scheduler`, `nova-conductor` and `cinder-*`
services. Two additional compute nodes run `nova-compute`. The walkthrough
uses a custom partitioning scheme that carves out 60 GB of space and labels it
as LVM. The network uses the `FlatManager` and `NetworkManager`
settings for OpenStack Compute.

The network mode does not interfere with OpenStack Block Storage
operations, but you must set up networking for Block Storage to work.
For details, see [networking](https://docs.openstack.org/neutron/latest/).

To set up Compute to use volumes, ensure that Block Storage is
installed along with `lvm2`. This guide describes how to
troubleshoot your installation and back up your Compute volumes.

Note

To enable the use of encrypted volumes, see the setup instructions in
[Create an encrypted volume type](https://docs.openstack.org/cinder/2025.2/configuration/block-storage/volume-encryption.html#create-encrypted-volume-type).
