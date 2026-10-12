use super::*;
use crate::tests::policy;
use std::time::Duration;

fn time() -> SampleTime {
    SampleTime {
        monotonic: Duration::from_secs(10),
        unix_seconds: 1700000000,
    }
}

fn node() -> NodeEvidence {
    NodeEvidence {
        inode: 100,
        links: 1,
        node_type: NodeType::RegularFile,
        owner_uid: 4242,
        owner_gid: 4242,
        mode: 0o600,
        filesystem: Some(policy().root.filesystem.clone()),
        project: Some(ProjectAttributes {
            project_id: ProjectId(4242),
            inheritance: false,
        }),
    }
}

#[test]
fn acquisition_source_replacement_binding_size_and_time_are_explicit_failures() {
    let policy = policy();
    let expected = node();
    let candidate = InventoryAcquisitionCandidate {
        before: expected.clone(),
        after: expected.clone(),
        transferred_bytes: 4096,
        policy_digest: policy.policy_digest().to_owned(),
        generation: policy.generation().to_owned(),
        acquired_at: time(),
    };
    assert!(candidate.check(&policy, &expected, time(), time()).is_ok());
    for mutate in [
        (|c: &mut InventoryAcquisitionCandidate| c.after.inode += 1)
            as fn(&mut InventoryAcquisitionCandidate),
        |c| c.before.owner_uid += 1,
        |c| c.before.links = 2,
        |c| c.before.mode = 0o666,
        |c| c.before.node_type = NodeType::Unsupported,
        |c| c.before.filesystem = None,
        |c| c.policy_digest = "0".repeat(64),
        |c| c.generation = "0".repeat(64),
        |c| c.transferred_bytes = 0,
        |c| c.transferred_bytes = MAX_INVENTORY_BYTES + 1,
        |c| c.acquired_at.monotonic = Duration::ZERO,
        |c| c.acquired_at.unix_seconds += 1,
    ] {
        let mut changed = candidate.clone();
        mutate(&mut changed);
        assert!(changed.check(&policy, &expected, time(), time()).is_err());
    }
}

#[test]
fn mount_source_text_or_equal_mount_id_cannot_replace_namespace_correspondence() {
    let mut expected = node();
    expected.node_type = NodeType::Directory;
    expected.links = 2;
    let candidate = MountedSourceCandidate {
        before: expected.clone(),
        mounted: expected.clone(),
        after: expected.clone(),
        consumer_namespace: expected.filesystem.as_ref().unwrap().namespace,
    };
    assert!(candidate.check(&expected).is_ok());
    for mutate in [
        (|c: &mut MountedSourceCandidate| c.consumer_namespace.inode += 1)
            as fn(&mut MountedSourceCandidate),
        |c| c.consumer_namespace.inode = 0,
        |c| c.mounted.inode += 1,
        |c| c.mounted.filesystem.as_mut().unwrap().namespace.inode += 1,
        |c| c.mounted.filesystem.as_mut().unwrap().device.minor += 1,
        |c| c.mounted.filesystem.as_mut().unwrap().uuid = "0".repeat(36),
        |c| c.after.inode += 1,
        |c| c.before.inode += 1,
    ] {
        let mut changed = candidate.clone();
        mutate(&mut changed);
        assert!(changed.check(&expected).is_err());
    }
}

#[test]
fn every_vfs_file_requires_matching_per_operation_descriptor_evidence() {
    let expected = node();
    for kind in [
        VfsFileKind::Database,
        VfsFileKind::Wal,
        VfsFileKind::Shm,
        VfsFileKind::Journal,
    ] {
        let candidate = VfsFileCandidate {
            kind,
            operation: VfsOperation::Open,
            before: expected.clone(),
            after: expected.clone(),
        };
        assert!(candidate.check(kind, VfsOperation::Open, &expected).is_ok());
        let wrong_kind = if kind == VfsFileKind::Database {
            VfsFileKind::Wal
        } else {
            VfsFileKind::Database
        };
        assert!(
            candidate
                .check(wrong_kind, VfsOperation::Open, &expected)
                .is_err()
        );
        for mutate in [
            (|c: &mut VfsFileCandidate| c.after.inode += 1) as fn(&mut VfsFileCandidate),
            |c| c.before.project.as_mut().unwrap().project_id.0 += 1,
            |c| c.after.filesystem.as_mut().unwrap().mount_id += 1,
            |c| c.before.links = 2,
            |c| c.after.mode = 0o666,
            |c| c.after.node_type = NodeType::Unsupported,
        ] {
            let mut changed = candidate.clone();
            mutate(&mut changed);
            assert!(changed.check(kind, VfsOperation::Open, &expected).is_err());
        }
    }
    let mut zero = expected.clone();
    zero.project.as_mut().unwrap().project_id.0 = 0;
    assert!(
        VfsFileCandidate {
            kind: VfsFileKind::Database,
            operation: VfsOperation::Open,
            before: zero.clone(),
            after: zero.clone()
        }
        .check(VfsFileKind::Database, VfsOperation::Open, &zero)
        .is_err()
    );
}

#[test]
fn vfs_operation_and_kind_are_bound_across_checkpoint_and_close_models() {
    let expected = node();
    for kind in [
        VfsFileKind::Database,
        VfsFileKind::Wal,
        VfsFileKind::Shm,
        VfsFileKind::Journal,
    ] {
        for operation in [
            VfsOperation::Open,
            VfsOperation::Create,
            VfsOperation::Read,
            VfsOperation::Write,
            VfsOperation::Truncate,
            VfsOperation::Lock,
            VfsOperation::Sync,
            VfsOperation::Checkpoint,
            VfsOperation::Close,
        ] {
            let mut candidate = VfsFileCandidate {
                kind,
                operation,
                before: expected.clone(),
                after: expected.clone(),
            };
            assert!(candidate.check(kind, operation, &expected).is_ok());
            let wrong = if operation == VfsOperation::Open {
                VfsOperation::Close
            } else {
                VfsOperation::Open
            };
            assert!(candidate.check(kind, wrong, &expected).is_err());
            candidate.after.inode += 1;
            assert!(candidate.check(kind, operation, &expected).is_err());
        }
    }
}

#[test]
fn effective_confinement_claims_are_required_but_do_not_install_a_policy() {
    let candidate = ConfinementCandidate {
        broker_effective_capabilities: 0,
        dedicated_identity: true,
        no_supplementary_groups: true,
        no_new_privileges: true,
        seccomp_enforcing: true,
        lsm_enforcing: true,
    };
    assert!(candidate.check().is_ok());
    for changed in [
        ConfinementCandidate {
            broker_effective_capabilities: 1,
            ..candidate
        },
        ConfinementCandidate {
            dedicated_identity: false,
            ..candidate
        },
        ConfinementCandidate {
            no_supplementary_groups: false,
            ..candidate
        },
        ConfinementCandidate {
            no_new_privileges: false,
            ..candidate
        },
        ConfinementCandidate {
            seccomp_enforcing: false,
            ..candidate
        },
        ConfinementCandidate {
            lsm_enforcing: false,
            ..candidate
        },
    ] {
        assert_eq!(
            changed.check(),
            Err(Failure::Unavailable(Unavailable::PermissionDenied))
        );
    }
}
