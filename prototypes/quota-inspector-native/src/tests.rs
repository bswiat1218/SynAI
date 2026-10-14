use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::os::fd::{AsRawFd, BorrowedFd, OwnedFd};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::path::PathBuf;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicU64, Ordering},
};
use std::time::Duration;

use quota_inspector_protocol::{StorageRole, inventory_generation};
use serde_json::{Value, json};

use super::*;
use crate::evidence::{
    DescriptorSet, InventoryAttestation, QueryProgress, RESOLUTION, ReadOnlyBackend,
    ResolutionRules,
};
use crate::observation::Inspection;

static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(0);

struct Disposable {
    directory: PathBuf,
    file: File,
}

impl Disposable {
    fn new() -> Self {
        let directory = std::env::temp_dir().join(format!(
            "synai-native-{}-{}",
            std::process::id(),
            NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed),
        ));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&directory)
            .unwrap();
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(directory.join("descriptor-seed"))
            .unwrap();
        Self { directory, file }
    }
}

impl Drop for Disposable {
    fn drop(&mut self) {
        std::fs::remove_file(self.directory.join("descriptor-seed")).unwrap();
        std::fs::remove_dir(&self.directory).unwrap();
    }
}

pub(crate) fn document() -> Value {
    let purposes = [
        ("task-slot-00", "task_slot", 256 * 1024 * 1024, 4096),
        ("task-slot-01", "task_slot", 256 * 1024 * 1024, 4096),
        ("task-slot-02", "task_slot", 256 * 1024 * 1024, 4096),
        ("task-slot-03", "task_slot", 256 * 1024 * 1024, 4096),
        ("graph", "podman_graph", 4u64 * 1024 * 1024 * 1024, 131_072),
        ("runtime", "podman_runtime", 512 * 1024 * 1024, 16_384),
        ("images", "image_scratch", 512 * 1024 * 1024, 16_384),
        ("state", "broker_state", 512 * 1024 * 1024, 16_384),
        ("logs", "broker_logs", 512 * 1024 * 1024, 8192),
        ("temporary", "temporary", 512 * 1024 * 1024, 16_384),
    ];
    let roles: Vec<_> = purposes
        .iter()
        .enumerate()
        .map(|(index, (id, purpose, bytes, inodes))| {
            json!({
                "role": {
                    "role_id": id, "kind": purpose, "path": format!("/synthetic/native/{id}"),
                    "mount_path": "/synthetic/native",
                    "filesystem_uuid": "11111111-2222-4333-8444-555555555555",
                    "project_id": 4242 + index, "byte_limit": bytes, "inode_limit": inodes,
                    "owner_uid": 4242, "owner_gid": 4242,
                },
                "purpose": purpose, "mode": 448,
                "existing_files": match *purpose {
                    "broker_state" => vec![
                        json!({"name":"broker.db","purpose":"sqlite_db","mode":384}),
                        json!({"name":"broker.db-wal","purpose":"sqlite_wal","mode":384}),
                        json!({"name":"broker.db-shm","purpose":"sqlite_shm","mode":384}),
                    ],
                    "broker_logs" => vec![json!({"name":"broker.log","purpose":"log","mode":384})],
                    _ => vec![],
                },
            })
        })
        .collect();
    let mut value = json!({
        "root": {
            "path": "/synthetic/native", "owner_uid": 0, "owner_gid": 0, "mode": 488,
            "filesystem": {
                "uuid": "11111111-2222-4333-8444-555555555555",
                "device": {"major": 253, "minor": 7},
                "mount_id": 42, "mount_root": "/", "filesystem_type": "ext4",
                "namespace": {"device": 4, "inode": 1001},
            },
        },
        "roles": roles, "inventory_generation": "",
    });
    regenerate(&mut value);
    value
}

fn regenerate(document: &mut Value) {
    let roles: Vec<StorageRole> = document["roles"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| serde_json::from_value(entry["role"].clone()).unwrap())
        .collect();
    document["inventory_generation"] = json!(inventory_generation(&roles));
}

fn candidate(value: &Value) -> Result<CandidateInventory, Failure> {
    CandidateInventory::parse(&serde_json::to_vec(value).unwrap())
}

pub(crate) fn policy() -> CandidateInventory {
    candidate(&document()).unwrap()
}

fn binding(policy: &CandidateInventory, role: &str) -> BoundRequest {
    BoundRequest::parse(
        policy,
        &serde_json::to_vec(&json!({
            "schema_version": 1, "message_type": "inspect_storage_role",
            "role_id": role, "challenge": "a".repeat(64),
            "inventory_generation": policy.generation(),
        }))
        .unwrap(),
    )
    .unwrap()
}

fn acquired_inventory(policy: &CandidateInventory) -> (AcquiredInventory, PathBuf) {
    let directory = std::env::temp_dir().join(format!(
        "synai-acquired-policy-{}-{}",
        std::process::id(),
        NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed),
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&directory)
        .unwrap();
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(directory.join("inventory.json"))
        .unwrap();
    std::io::Write::write_all(&mut file, &serde_json::to_vec(&document()).unwrap()).unwrap();
    drop(file);
    let metadata = std::fs::metadata(&directory).unwrap();
    let acquired = AcquiredInventory::load_test_root(
        &directory,
        metadata.uid(),
        metadata.gid(),
        0o700,
        0o600,
        std::time::Instant::now(),
    )
    .unwrap();
    assert_eq!(acquired.policy().policy_digest(), policy.policy_digest());
    (acquired, directory)
}

type JobLease = Arc<Mutex<Option<Arc<DescriptorSet>>>>;
type SupervisorLease = Arc<Mutex<crate::supervision::WorkerLease<DescriptorSet>>>;

struct FakeBackend {
    disposable: Disposable,
    nodes: HashMap<i32, (String, NodeEvidence, usize)>,
    approved_digest: String,
    generation: String,
    filesystem: FilesystemIdentity,
    project: ProjectId,
    quota: QuotaEvidence,
    time: SampleTime,
    attestation_mutator: Option<fn(&mut InventoryAttestation)>,
    attestation_delay: Duration,
    node_mutator: Option<fn(&mut NodeEvidence)>,
    node_target: Option<String>,
    mutate_after: usize,
    query_error: Option<Failure>,
    binding_error: bool,
    node_error: Option<Failure>,
    open_error: Option<Failure>,
    pending: bool,
    completed: bool,
    job: JobLease,
    query_calls: usize,
    open_names: Vec<String>,
    supervised: bool,
    supervisor: Option<SupervisorLease>,
}

impl FakeBackend {
    fn new(policy: &CandidateInventory, role_id: &str) -> Self {
        let role = policy.role(role_id).unwrap();
        let time = SampleTime {
            monotonic: Duration::from_secs(10),
            unix_seconds: 1_700_000_000,
        };
        let reading = QuotaReading {
            project_id: ProjectId(role.role.project_id as u32),
            filesystem: policy.root.filesystem.clone(),
            state: QuotaState::Enforced,
            limits: HardLimits {
                bytes: role.role.byte_limit,
                inodes: role.role.inode_limit,
            },
            usage: Usage {
                bytes: 64,
                inodes: 1,
            },
            capacity: Capacity {
                blocks: 8_388_608,
                available_blocks: 7_864_320,
                fragment_bytes: 4096,
                inodes: 1_048_576,
                available_inodes: 1_000_000,
            },
        };
        Self {
            disposable: Disposable::new(),
            nodes: HashMap::new(),
            approved_digest: policy.policy_digest().to_owned(),
            generation: policy.generation().to_owned(),
            filesystem: policy.root.filesystem.clone(),
            project: reading.project_id,
            quota: QuotaEvidence {
                before: reading.clone(),
                after: reading,
                sampled_at: time,
            },
            time,
            attestation_mutator: None,
            attestation_delay: Duration::ZERO,
            node_mutator: None,
            node_target: None,
            mutate_after: 0,
            query_error: None,
            binding_error: false,
            node_error: None,
            open_error: None,
            pending: false,
            completed: false,
            job: Arc::new(Mutex::new(None)),
            query_calls: 0,
            open_names: vec![],
            supervised: false,
            supervisor: None,
        }
    }

    fn descriptor(&mut self, name: &str, evidence: NodeEvidence) -> OwnedFd {
        let file = self.disposable.file.try_clone().unwrap();
        self.nodes
            .insert(file.as_raw_fd(), (name.to_owned(), evidence, 0));
        file.into()
    }
}

impl ReadOnlyBackend for FakeBackend {
    fn clock(&self) -> SampleTime {
        self.time
    }

    fn attest(&mut self, _: &CandidateInventory) -> Result<InventoryAttestation, Failure> {
        self.time.monotonic += self.attestation_delay;
        let mut attestation = InventoryAttestation {
            policy_digest: self.approved_digest.clone(),
            generation: self.generation.clone(),
            approval_identity: "b".repeat(64),
            consumer_namespace: Some(self.filesystem.namespace),
            acquired_at: self.time,
        };
        if let Some(mutate) = self.attestation_mutator {
            mutate(&mut attestation);
        }
        Ok(attestation)
    }

    fn trusted_root(&mut self, policy: &TrustedInventory) -> Result<OwnedFd, Failure> {
        Ok(self.descriptor(
            "root",
            NodeEvidence {
                inode: 100,
                links: 2,
                node_type: NodeType::Directory,
                owner_uid: policy.policy.root.owner_uid,
                owner_gid: policy.policy.root.owner_gid,
                mode: policy.policy.root.mode,
                filesystem: Some(self.filesystem.clone()),
                project: None,
            },
        ))
    }

    fn root_still_attested(
        &mut self,
        _: &TrustedInventory,
        _: BorrowedFd<'_>,
    ) -> Result<(), Failure> {
        if self.binding_error {
            Err(Failure::Inconsistent(Inconsistent::NodeIdentity))
        } else {
            Ok(())
        }
    }

    fn open_component(
        &mut self,
        parent: BorrowedFd<'_>,
        name: &str,
        rules: ResolutionRules,
    ) -> Result<OwnedFd, Failure> {
        assert_eq!(rules, RESOLUTION);
        assert!(crate::inventory::component(name));
        assert!(self.nodes.contains_key(&parent.as_raw_fd()));
        if let Some(error) = self.open_error {
            return Err(error);
        }
        self.open_names.push(name.to_owned());
        let regular = name.starts_with("broker.");
        Ok(self.descriptor(
            name,
            NodeEvidence {
                inode: 100 + self.open_names.len() as u64,
                links: if regular { 1 } else { 2 },
                node_type: if regular {
                    NodeType::RegularFile
                } else {
                    NodeType::Directory
                },
                owner_uid: 4242,
                owner_gid: 4242,
                mode: if regular { 0o600 } else { 0o700 },
                filesystem: Some(self.filesystem.clone()),
                project: Some(ProjectAttributes {
                    project_id: self.project,
                    inheritance: !regular,
                }),
            },
        ))
    }

    fn node(&mut self, descriptor: BorrowedFd<'_>) -> Result<NodeEvidence, Failure> {
        if let Some(error) = self.node_error {
            return Err(error);
        }
        let (name, initial, reads) = self
            .nodes
            .get_mut(&descriptor.as_raw_fd())
            .ok_or(Failure::Unavailable(Unavailable::DescriptorUnavailable))?;
        let mut evidence = initial.clone();
        if self
            .node_target
            .as_ref()
            .is_none_or(|target| target == name)
            && *reads >= self.mutate_after
            && let Some(mutate) = self.node_mutator
        {
            mutate(&mut evidence);
        }
        *reads += 1;
        Ok(evidence)
    }

    fn same_entry(
        &mut self,
        _: BorrowedFd<'_>,
        _: &str,
        _: BorrowedFd<'_>,
        rules: ResolutionRules,
    ) -> Result<(), Failure> {
        assert_eq!(rules, RESOLUTION);
        if self.binding_error {
            Err(Failure::Inconsistent(Inconsistent::NodeIdentity))
        } else {
            Ok(())
        }
    }

    fn read_project_quota(
        &mut self,
        descriptor: BorrowedFd<'_>,
        project: ProjectId,
        lease: Arc<DescriptorSet>,
    ) -> QueryProgress {
        assert_eq!(self.nodes[&descriptor.as_raw_fd()].0, "root");
        assert_eq!(project, self.project);
        assert!(lease.nodes.len() <= MAX_DESCRIPTORS);
        self.query_calls += 1;
        let pending_reason = match self.query_error {
            Some(Failure::Unavailable(
                reason @ (Unavailable::Blocked
                | Unavailable::UnresolvedOwnership
                | Unavailable::DeadlineExceeded
                | Unavailable::Cancelled),
            )) => Some(reason),
            _ => None,
        };
        if self.pending || pending_reason.is_some() {
            if self.supervised {
                self.supervisor = Some(Arc::new(Mutex::new(
                    crate::supervision::WorkerLease::new(worker_identity(), Arc::clone(&lease))
                        .unwrap(),
                )));
            }
            *self.job.lock().unwrap() = Some(lease);
            return QueryProgress::Pending(pending_reason.unwrap_or(Unavailable::Blocked));
        }
        if let Some(error) = self.query_error {
            return QueryProgress::Complete(Err(error));
        }
        QueryProgress::Complete(Ok(Box::new(self.quota.clone())))
    }

    fn poll_quota(&mut self) -> QueryProgress {
        if !self.completed {
            let reason = match self.query_error {
                Some(Failure::Unavailable(reason)) => reason,
                _ => Unavailable::Blocked,
            };
            return QueryProgress::Pending(reason);
        }
        if let Some(supervisor) = &self.supervisor
            && supervisor.lock().unwrap().finalize().is_err()
        {
            return QueryProgress::Pending(Unavailable::UnresolvedOwnership);
        }
        self.job.lock().unwrap().take();
        if let Some(error) = self.query_error {
            return QueryProgress::Complete(Err(error));
        }
        QueryProgress::Complete(Ok(Box::new(self.quota.clone())))
    }
}
fn worker_identity() -> crate::supervision::WorkerIdentity {
    crate::supervision::WorkerIdentity {
        pid: 4242,
        start_ticks: 100,
        generation: 1,
        boot_epoch: [1; 16],
        pid_namespace: NamespaceId {
            device: 4,
            inode: 2001,
        },
    }
}

#[test]
fn inventory_attestation_requires_fresh_per_request_acquisition() {
    for mutate in [
        (|attestation: &mut InventoryAttestation| {
            attestation.acquired_at.monotonic -= Duration::from_nanos(1);
        }) as fn(&mut InventoryAttestation),
        |attestation| attestation.acquired_at.monotonic += Duration::from_nanos(1),
        |attestation| attestation.acquired_at.unix_seconds += 1,
        |attestation| attestation.acquired_at.unix_seconds -= 6,
    ] {
        let policy = policy();
        let mut backend = FakeBackend::new(&policy, "state");
        backend.attestation_mutator = Some(mutate);
        assert_eq!(
            operation(backend, &policy, "state").err(),
            Some(Failure::Unavailable(Unavailable::StaleEvidence))
        );
    }
}

#[test]
fn exact_worker_finalization_is_required_before_native_completion() {
    for terminal in [Unavailable::Cancelled, Unavailable::DeadlineExceeded] {
        let policy = policy();
        let mut backend = FakeBackend::new(&policy, "state");
        backend.pending = true;
        backend.supervised = true;
        let mut inspection = operation(backend, &policy, "state").unwrap();
        assert!(inspection.poll().is_err());
        let supervisor = Arc::clone(inspection.backend.supervisor.as_ref().unwrap());
        if terminal == Unavailable::Cancelled {
            inspection.cancel();
        } else {
            inspection.backend.time.monotonic += MAX_OPERATION_TIME;
        }
        inspection.backend.completed = true; // output/cancellation is not physical completion.
        let id = worker_identity();
        for wrong in [
            crate::supervision::WorkerIdentity {
                pid: id.pid + 1,
                ..id
            },
            crate::supervision::WorkerIdentity {
                start_ticks: id.start_ticks + 1,
                ..id
            },
            crate::supervision::WorkerIdentity {
                generation: id.generation + 1,
                ..id
            },
            crate::supervision::WorkerIdentity {
                boot_epoch: [2; 16],
                ..id
            },
            crate::supervision::WorkerIdentity {
                pid_namespace: NamespaceId {
                    inode: 2002,
                    ..id.pid_namespace
                },
                ..id
            },
        ] {
            assert!(supervisor.lock().unwrap().exited(wrong).is_err());
        }
        assert!(supervisor.lock().unwrap().reaped(id).is_err());
        assert!(supervisor.lock().unwrap().cleaned(id).is_err());
        for _ in 0..5 {
            assert_eq!(
                inspection.poll().unwrap_err(),
                Failure::Unavailable(terminal)
            );
            assert!(inspection.pending());
            assert!(inspection.descriptor_count() > 0);
        }
        supervisor.lock().unwrap().exited(id).unwrap();
        assert!(inspection.poll().is_err());
        assert!(inspection.pending());
        supervisor.lock().unwrap().reaped(id).unwrap();
        assert!(inspection.poll().is_err());
        assert!(inspection.pending());
        supervisor.lock().unwrap().cleaned(id).unwrap();
        assert_eq!(
            inspection.poll().unwrap_err(),
            Failure::Unavailable(terminal)
        );
        assert!(!inspection.pending());
        assert_eq!(inspection.descriptor_count(), 0);
        assert!(inspection.backend.job.lock().unwrap().is_none());
        let fresh = FakeBackend::new(&policy, "state");
        assert!(operation(fresh, &policy, "state").unwrap().poll().is_ok());
    }
}

#[test]
fn simulated_supervisor_survives_dropped_request_and_crash_until_reaped() {
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.pending = true;
    backend.supervised = true;
    let mut inspection = operation(backend, &policy, "state").unwrap();
    assert!(inspection.poll().is_err());
    let supervisor = Arc::clone(inspection.backend.supervisor.as_ref().unwrap());
    let job = Arc::clone(&inspection.backend.job);
    let retained = Arc::downgrade(job.lock().unwrap().as_ref().unwrap());
    inspection.cancel();
    drop(inspection);
    job.lock().unwrap().take();
    assert!(retained.upgrade().is_some());
    let id = worker_identity();
    // Reconciliation has no completion evidence yet: no new worker/lease release.
    assert!(supervisor.lock().unwrap().finalize().is_err());
    supervisor.lock().unwrap().exited(id).unwrap();
    assert!(supervisor.lock().unwrap().finalize().is_err());
    supervisor.lock().unwrap().reaped(id).unwrap();
    assert!(supervisor.lock().unwrap().finalize().is_err());
    supervisor.lock().unwrap().cleaned(id).unwrap();
    supervisor.lock().unwrap().finalize().unwrap();
    assert!(retained.upgrade().is_none());
}
fn operation(
    backend: FakeBackend,
    selected: &CandidateInventory,
    id: &str,
) -> Result<Inspection<FakeBackend>, Failure> {
    Inspection::begin(
        selected,
        &binding(selected, id),
        ObservationBudget::new(MAX_OPERATION_TIME).unwrap(),
        backend,
    )
}

fn fail_query(mutate: fn(&mut QuotaEvidence), expected: Failure) {
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    mutate(&mut backend.quota);
    let mut operation = operation(backend, &policy, "state").unwrap();
    assert_eq!(operation.poll().unwrap_err(), expected);
    assert!(!operation.pending());
}

fn fail_node(mutate: fn(&mut NodeEvidence), target: &str, expected: Failure, after: usize) {
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.node_mutator = Some(mutate);
    backend.node_target = Some(target.to_owned());
    backend.mutate_after = after;
    match operation(backend, &policy, "state") {
        Err(error) => assert_eq!(error, expected),
        Ok(mut operation) => {
            assert_eq!(operation.poll().unwrap_err(), expected);
            assert!(!operation.pending());
        }
    }
}

#[test]
fn inventory_requires_all_purposes_and_distinct_project_domains() {
    let mut value = document();
    assert_eq!(candidate(&value).unwrap().role_count(), 10);
    value["roles"][1]["role"]["project_id"] = value["roles"][0]["role"]["project_id"].clone();
    regenerate(&mut value);
    assert!(candidate(&value).is_err());
    value = document();
    value["roles"].as_array_mut().unwrap().pop();
    regenerate(&mut value);
    assert!(candidate(&value).is_err());
}

#[test]
fn inventory_rejects_path_aliases_overlaps_and_shared_ancestor_policy_conflicts() {
    for path in [
        "/synthetic/native/task-slot-00",
        "/synthetic/native/task-slot-00/child",
    ] {
        let mut value = document();
        value["roles"][1]["role"]["path"] = json!(path);
        regenerate(&mut value);
        assert_eq!(
            candidate(&value).unwrap_err(),
            Failure::Inconsistent(Inconsistent::AliasedDomain)
        );
    }
    let mut value = document();
    value["roles"][0]["role"]["path"] = json!("/synthetic/native/tasks/a");
    value["roles"][1]["role"]["path"] = json!("/synthetic/native/tasks/b");
    value["roles"][1]["mode"] = json!(0o750);
    regenerate(&mut value);
    assert!(candidate(&value).is_err());
}

#[test]
fn inventory_rejects_unbounded_invalid_and_unsafe_inputs() {
    for (field, replacement) in [
        ("path", json!("/synthetic/native/../escape")),
        ("path", json!("/elsewhere/role")),
        ("path", json!("/synthetic/native/a//b")),
        ("path", json!("/synthetic/native/a/")),
        ("path", json!("/synthetic/native/\u{0}")),
        ("project_id", json!(0)),
        ("project_id", json!(u64::MAX)),
        ("byte_limit", json!(u64::MAX)),
        ("byte_limit", json!(1043)),
        ("inode_limit", json!(0)),
        ("owner_uid", json!(u64::MAX)),
        ("kind", json!("unknown")),
    ] {
        let mut value = document();
        value["roles"][0]["role"][field] = replacement;
        regenerate(&mut value);
        assert!(candidate(&value).is_err(), "{field}");
    }
    for mode in [0o777, 0o1700, 0] {
        let mut value = document();
        value["roles"][0]["mode"] = json!(mode);
        assert!(candidate(&value).is_err());
    }
    assert!(CandidateInventory::parse(&vec![b' '; MAX_INVENTORY_BYTES + 1]).is_err());
    assert!(CandidateInventory::parse(b"\xff").is_err());
    assert!(CandidateInventory::parse(b"{\"root\":null}").is_err());
    assert!(CandidateInventory::parse(&[b"[".repeat(17), b"]".repeat(17)].concat()).is_err());
}

#[test]
fn inventory_rejects_duplicate_fields_null_unknowns_and_generation_mismatch() {
    let text = serde_json::to_string(&document()).unwrap();
    let duplicate = text.replacen(
        "\"owner_uid\":4242",
        "\"owner_uid\":4242,\"owner_uid\":4242",
        1,
    );
    assert!(CandidateInventory::parse(duplicate.as_bytes()).is_err());
    for change in ["{\"extra\":null,", "{\"inventory_generation\":null,"] {
        let malformed = text.replacen('{', change, 1);
        assert!(CandidateInventory::parse(malformed.as_bytes()).is_err());
    }
    let mut value = document();
    value["inventory_generation"] = json!("0".repeat(64));
    assert!(candidate(&value).is_err());
}

#[test]
fn inventory_binds_modes_and_existing_files_separately_from_v1_generation() {
    let first = candidate(&document()).unwrap();
    let mut value = document();
    value["roles"][0]["mode"] = json!(0o750);
    let second = candidate(&value).unwrap();
    assert_eq!(first.generation(), second.generation());
    assert_ne!(first.policy_digest(), second.policy_digest());
}

#[test]
fn inventory_requires_db_wal_shm_and_log_checks() {
    for index in [7, 8] {
        let mut value = document();
        value["roles"][index]["existing_files"]
            .as_array_mut()
            .unwrap()
            .pop();
        assert!(candidate(&value).is_err());
    }
    for replacement in [
        json!({"name":"../db","purpose":"sqlite_db","mode":384}),
        json!({"name":"broker.db","purpose":"log","mode":384}),
        json!({"name":"broker.db","purpose":"sqlite_db","mode":420}),
    ] {
        let mut value = document();
        value["roles"][7]["existing_files"][0] = replacement;
        assert!(candidate(&value).is_err());
    }
}

#[test]
fn canonical_identity_and_storage_planning_remain_unchanged() {
    let document = document();
    let roles: Vec<StorageRole> = document["roles"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| serde_json::from_value(entry["role"].clone()).unwrap())
        .collect();
    assert_eq!(
        roles.iter().map(|role| role.byte_limit).sum::<u64>(),
        HARD_ENTITLEMENTS
    );
    assert_eq!(
        roles.iter().map(|role| role.inode_limit).sum::<u64>(),
        HARD_INODE_ENTITLEMENTS
    );
    assert_eq!(
        HARD_ENTITLEMENTS + CONTINUOUS_RESERVE + OPERATIONAL_ALLOWANCE,
        INITIAL_AVAILABLE_BYTES
    );
    assert_eq!(INITIAL_AVAILABLE_INODES, 241_664);
    assert_eq!(MAX_PLANNED_STORAGE, 128 * 1024 * 1024 * 1024);
    let mut reversed = roles.clone();
    reversed.reverse();
    assert_eq!(
        inventory_generation(&roles),
        inventory_generation(&reversed)
    );
}

#[test]
fn default_native_cannot_observe_or_publish_configured_success() {
    let policy = policy();
    let request = binding(&policy, "state");
    let mut native = NativeInspector::new();
    let result = native.inspect(
        &policy,
        &request,
        ObservationBudget::new(MAX_OPERATION_TIME).unwrap(),
    );
    assert!(matches!(
        result.outcome(),
        Outcome::Failed(Failure::Unavailable(
            Unavailable::InventoryAuthorityUnavailable
        ))
    ));
    let wire: Value =
        serde_json::from_slice(&result.protocol_response(&"c".repeat(32)).unwrap()).unwrap();
    assert_eq!(wire["status"], "unavailable");
    assert_eq!(wire["error_code"], "inventory_authority_unavailable");
    assert_eq!(wire.as_object().unwrap().len(), 8);
    assert!(wire.get("observed_at").is_none());
    assert!(native.poll().is_none());
}

#[test]
fn request_validation_is_exact_and_cannot_select_filesystem_authority() {
    let policy = policy();
    let template = json!({
        "schema_version":1, "message_type":"inspect_storage_role", "role_id":"state",
        "challenge":"a".repeat(64), "inventory_generation":policy.generation(),
    });
    for (field, value) in [
        ("path", json!("/etc")),
        ("command", json!("quota")),
        ("project_id", json!(1)),
        ("schema_version", json!(true)),
        ("challenge", json!("A".repeat(64))),
        ("role_id", json!("../state")),
        ("inventory_generation", json!("0".repeat(64))),
    ] {
        let mut value = {
            let mut candidate = template.clone();
            candidate[field] = value;
            candidate
        };
        assert!(BoundRequest::parse(&policy, &serde_json::to_vec(&value).unwrap()).is_err());
        value.as_object_mut().unwrap().remove("challenge");
        assert!(BoundRequest::parse(&policy, &serde_json::to_vec(&value).unwrap()).is_err());
    }
    let duplicate =
        serde_json::to_string(&template)
            .unwrap()
            .replacen('{', "{\"role_id\":\"state\",", 1);
    assert!(BoundRequest::parse(&policy, duplicate.as_bytes()).is_err());
}

#[test]
fn complete_fake_pipeline_checks_all_existing_files_and_exact_wire_schema() {
    for id in ["state", "logs", "task-slot-00"] {
        let policy = policy();
        let backend = FakeBackend::new(&policy, id);
        let mut operation = operation(backend, &policy, id).unwrap();
        assert!(operation.descriptor_count() <= MAX_DESCRIPTORS);
        let observation = operation.poll().unwrap();
        assert!(!operation.pending());
        assert_eq!(operation.backend.query_calls, 1);
        let result = crate::observation::InspectionResult::from_test_observation(
            binding(&policy, id),
            observation,
        );
        let wire: Value =
            serde_json::from_slice(&result.protocol_response(&"c".repeat(32)).unwrap()).unwrap();
        assert_eq!(wire["status"], "ok");
        assert_eq!(wire.as_object().unwrap().len(), 26);
        assert!(wire.get("error_code").is_none());
        assert_eq!(wire["bytes_used"], 64);
    }
}

#[test]
fn matching_digest_is_not_inventory_authority_and_attestation_is_fenced() {
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.attestation_mutator = Some(|attestation| attestation.policy_digest = "0".repeat(64));
    assert_eq!(
        operation(backend, &policy, "state").err(),
        Some(Failure::Inconsistent(Inconsistent::InventoryBinding))
    );
    let mut backend = FakeBackend::new(&policy, "state");
    backend.attestation_mutator = Some(|attestation| attestation.approval_identity.clear());
    assert!(operation(backend, &policy, "state").is_err());
}

#[test]
fn namespace_ids_are_not_global_mount_ids() {
    let policy = policy();
    for mutation in [
        (|attestation: &mut InventoryAttestation| attestation.consumer_namespace = None)
            as fn(&mut InventoryAttestation),
        |attestation| attestation.consumer_namespace.as_mut().unwrap().inode += 1,
    ] {
        let mut backend = FakeBackend::new(&policy, "state");
        backend.attestation_mutator = Some(mutation);
        assert!(operation(backend, &policy, "state").is_err());
    }
    fail_node(
        |node| node.filesystem.as_mut().unwrap().namespace.inode += 1,
        "state",
        Failure::Inconsistent(Inconsistent::NamespaceMismatch),
        0,
    );
    fail_query(
        |quota| {
            quota.after.filesystem.namespace.inode += 1;
            quota.before = quota.after.clone();
        },
        Failure::Inconsistent(Inconsistent::NamespaceMismatch),
    );
}

#[test]
fn filesystem_uuid_device_mount_root_id_and_type_are_all_checked() {
    for mutate in [
        (|node: &mut NodeEvidence| {
            node.filesystem.as_mut().unwrap().uuid = "aaaaaaaa-2222-4333-8444-555555555555".into()
        }) as fn(&mut NodeEvidence),
        |node| node.filesystem.as_mut().unwrap().device.minor += 1,
        |node| node.filesystem.as_mut().unwrap().mount_id += 1,
        |node| node.filesystem.as_mut().unwrap().mount_root = "/bind".into(),
        |node| node.filesystem.as_mut().unwrap().filesystem_type = "other".into(),
    ] {
        fail_node(
            mutate,
            "state",
            Failure::Inconsistent(Inconsistent::FilesystemIdentity),
            0,
        );
        fail_node(
            mutate,
            "state",
            Failure::Inconsistent(Inconsistent::FilesystemIdentity),
            2,
        );
    }
}

#[test]
fn directory_owner_mode_inode_type_and_replacement_are_checked() {
    fail_node(
        |node| node.owner_uid += 1,
        "state",
        Failure::Inconsistent(Inconsistent::Ownership),
        0,
    );
    fail_node(
        |node| node.owner_gid += 1,
        "state",
        Failure::Inconsistent(Inconsistent::Ownership),
        2,
    );
    fail_node(
        |node| node.mode = 0o777,
        "state",
        Failure::Inconsistent(Inconsistent::Ownership),
        0,
    );
    fail_node(
        |node| node.node_type = NodeType::Unsupported,
        "state",
        Failure::Inconsistent(Inconsistent::NodeIdentity),
        0,
    );
    fail_node(
        |node| node.inode += 1,
        "state",
        Failure::Inconsistent(Inconsistent::NodeIdentity),
        2,
    );
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.binding_error = true;
    assert!(operation(backend, &policy, "state").is_err());
}

#[test]
fn directory_project_and_inheritance_are_not_file_project_evidence() {
    fail_node(
        |node| node.project.as_mut().unwrap().project_id.0 += 1,
        "state",
        Failure::Inconsistent(Inconsistent::ProjectIdentity),
        0,
    );
    fail_node(
        |node| node.project.as_mut().unwrap().inheritance = false,
        "state",
        Failure::Inconsistent(Inconsistent::Inheritance),
        0,
    );
    for name in ["broker.db", "broker.db-wal", "broker.db-shm"] {
        fail_node(
            |node| node.project.as_mut().unwrap().project_id.0 += 1,
            name,
            Failure::Inconsistent(Inconsistent::ProjectIdentity),
            0,
        );
    }
}

#[test]
fn existing_files_require_single_links_exact_owner_mode_mount_and_stable_inode() {
    fail_node(
        |node| node.links = 2,
        "broker.db",
        Failure::Inconsistent(Inconsistent::HardLinks),
        0,
    );
    fail_node(
        |node| node.owner_uid += 1,
        "broker.db-wal",
        Failure::Inconsistent(Inconsistent::Ownership),
        0,
    );
    fail_node(
        |node| node.mode = 0o644,
        "broker.db-shm",
        Failure::Inconsistent(Inconsistent::Ownership),
        0,
    );
    fail_node(
        |node| node.filesystem.as_mut().unwrap().mount_id += 1,
        "broker.db",
        Failure::Inconsistent(Inconsistent::FilesystemIdentity),
        0,
    );
    fail_node(
        |node| node.inode += 1,
        "broker.db",
        Failure::Inconsistent(Inconsistent::NodeIdentity),
        2,
    );
}

#[test]
fn missing_or_malformed_descriptor_evidence_never_defaults_to_success() {
    fail_node(
        |node| node.project = None,
        "state",
        Failure::Unavailable(Unavailable::MissingEvidence),
        0,
    );
    fail_node(
        |node| node.filesystem = None,
        "state",
        Failure::Unavailable(Unavailable::MissingEvidence),
        0,
    );
    fail_node(
        |node| node.inode = 0,
        "state",
        Failure::Unavailable(Unavailable::MalformedEvidence),
        0,
    );
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.node_error = Some(Failure::Unavailable(Unavailable::DescriptorUnavailable));
    assert!(operation(backend, &policy, "state").is_err());
    let mut backend = FakeBackend::new(&policy, "state");
    backend.open_error = Some(Failure::Unavailable(Unavailable::MissingEvidence));
    assert!(operation(backend, &policy, "state").is_err());
}

#[test]
fn unsupported_permission_missing_and_malformed_queries_remain_unavailable() {
    let policy = policy();
    for reason in [
        Unavailable::UnsupportedInterface,
        Unavailable::PermissionDenied,
        Unavailable::MissingEvidence,
        Unavailable::MalformedEvidence,
    ] {
        let mut backend = FakeBackend::new(&policy, "state");
        backend.query_error = Some(Failure::Unavailable(reason));
        let mut operation = operation(backend, &policy, "state").unwrap();
        assert_eq!(operation.poll().unwrap_err(), Failure::Unavailable(reason));
        assert!(!operation.pending());
    }
}

#[test]
fn disabled_accounting_only_and_unknown_quota_state_fail_closed() {
    fail_query(
        |quota| {
            quota.after.state = QuotaState::Disabled;
            quota.before = quota.after.clone();
        },
        Failure::Inconsistent(Inconsistent::QuotaNotEnforced),
    );
    fail_query(
        |quota| {
            quota.after.state = QuotaState::AccountingOnly;
            quota.before = quota.after.clone();
        },
        Failure::Inconsistent(Inconsistent::QuotaNotEnforced),
    );
    fail_query(
        |quota| {
            quota.after.state = QuotaState::Unknown;
            quota.before = quota.after.clone();
        },
        Failure::Unavailable(Unavailable::MissingEvidence),
    );
}

#[test]
fn quota_project_limits_usage_and_free_capacity_are_independent_checks() {
    fail_query(
        |quota| {
            quota.after.project_id.0 += 1;
            quota.before = quota.after.clone();
        },
        Failure::Inconsistent(Inconsistent::ProjectIdentity),
    );
    fail_query(
        |quota| {
            quota.after.limits.bytes += 1;
            quota.before = quota.after.clone();
        },
        Failure::Inconsistent(Inconsistent::HardLimits),
    );
    fail_query(
        |quota| {
            quota.after.limits.inodes += 1;
            quota.before = quota.after.clone();
        },
        Failure::Inconsistent(Inconsistent::HardLimits),
    );
    fail_query(
        |quota| {
            quota.after.usage.bytes = quota.after.limits.bytes + 1;
            quota.before = quota.after.clone();
        },
        Failure::Inconsistent(Inconsistent::Usage),
    );
    fail_query(
        |quota| {
            quota.after.usage.inodes = quota.after.limits.inodes + 1;
            quota.before = quota.after.clone();
        },
        Failure::Inconsistent(Inconsistent::Usage),
    );
    fail_query(
        |quota| {
            quota.after.capacity.available_blocks = quota.after.capacity.blocks + 1;
            quota.before = quota.after.clone();
        },
        Failure::Inconsistent(Inconsistent::Usage),
    );
}

#[test]
fn quota_arithmetic_overflow_and_zero_units_are_not_observations() {
    fail_query(
        |quota| {
            quota.after.capacity.blocks = u64::MAX;
            quota.before = quota.after.clone();
        },
        Failure::Unavailable(Unavailable::ArithmeticOverflow),
    );
    fail_query(
        |quota| {
            quota.after.capacity.fragment_bytes = 0;
            quota.before = quota.after.clone();
        },
        Failure::Unavailable(Unavailable::MalformedEvidence),
    );
}

#[test]
fn stale_future_and_preoperation_samples_are_unavailable() {
    fail_query(
        |quota| quota.sampled_at.unix_seconds -= 6,
        Failure::Unavailable(Unavailable::StaleEvidence),
    );
    fail_query(
        |quota| quota.sampled_at.unix_seconds += 3,
        Failure::Unavailable(Unavailable::StaleEvidence),
    );
    fail_query(
        |quota| quota.sampled_at.monotonic -= Duration::from_secs(1),
        Failure::Unavailable(Unavailable::StaleEvidence),
    );
}

#[test]
fn prepost_quota_drift_is_inconsistent() {
    fail_query(
        |quota| quota.after.usage.bytes += 1,
        Failure::Inconsistent(Inconsistent::ChangedEvidence),
    );
}

#[test]
fn deadline_and_cancellation_before_query_release_idle_descriptors() {
    let policy = policy();
    let mut cancelled = operation(FakeBackend::new(&policy, "state"), &policy, "state").unwrap();
    cancelled.cancel();
    assert_eq!(
        cancelled.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::Cancelled)
    );
    assert!(!cancelled.pending());
    assert_eq!(cancelled.backend.query_calls, 0);
    let mut operation = operation(FakeBackend::new(&policy, "state"), &policy, "state").unwrap();
    operation.backend.time.monotonic += MAX_OPERATION_TIME;
    assert_eq!(
        operation.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::DeadlineExceeded)
    );
    assert!(!operation.pending());
}

#[test]
fn blocked_query_cancellation_retains_owned_descriptors_until_confirmed_completion() {
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.pending = true;
    let job = Arc::clone(&backend.job);
    let mut operation = operation(backend, &policy, "state").unwrap();
    assert_eq!(
        operation.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::Blocked)
    );
    let count = operation.descriptor_count();
    operation.cancel();
    assert_eq!(
        operation.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::Cancelled)
    );
    assert_eq!(operation.descriptor_count(), count);
    assert!(job.lock().unwrap().is_some());
    operation.backend.completed = true;
    assert_eq!(
        operation.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::Cancelled)
    );
    assert!(!operation.pending());
    assert!(job.lock().unwrap().is_none());
}

#[test]
fn acquired_inventory_descriptor_is_retained_by_the_worker_lease_until_reaped() {
    let policy = policy();
    let request = binding(&policy, "state");
    let (acquired, source_directory) = acquired_inventory(&policy);
    let source_fd = acquired.source_fd().as_raw_fd();
    let worker_job = Arc::new(Mutex::new(None));
    let mut backend = FakeBackend::new(&policy, "state");
    backend.pending = true;
    backend.job = Arc::clone(&worker_job);
    let mut controller = crate::observation::Controller { active: None };
    let budget = ObservationBudget::new(MAX_OPERATION_TIME).unwrap();

    let first = controller.inspect_acquired(acquired, &request, budget, backend);
    assert!(matches!(
        first.outcome(),
        Outcome::Failed(Failure::Unavailable(Unavailable::Blocked))
    ));
    let lease = worker_job.lock().unwrap().as_ref().unwrap().clone();
    assert!(lease._source_inventory.is_some());
    assert!(std::fs::metadata(format!("/proc/self/fd/{source_fd}")).is_ok());
    std::fs::remove_file(source_directory.join("inventory.json")).unwrap();
    std::fs::remove_dir(&source_directory).unwrap();

    controller.cancel();
    let pending = controller.poll().unwrap();
    assert!(matches!(
        pending.outcome(),
        Outcome::Failed(Failure::Unavailable(Unavailable::Cancelled))
    ));
    drop(controller);
    assert!(std::fs::metadata(format!("/proc/self/fd/{source_fd}")).is_ok());

    worker_job.lock().unwrap().take();
    drop(lease);
    assert!(std::fs::metadata(format!("/proc/self/fd/{source_fd}")).is_err());
}

#[test]
fn acquired_inventory_handoff_rejects_a_policy_digest_mismatch() {
    let policy = policy();
    let (acquired, source_directory) = acquired_inventory(&policy);
    let mut changed = document();
    changed["root"]["mode"] = json!(0o700);
    let changed_policy = candidate(&changed).unwrap();
    assert_eq!(changed_policy.generation(), policy.generation());
    assert_ne!(changed_policy.policy_digest(), policy.policy_digest());
    assert_eq!(
        acquired.validate_binding(&changed_policy),
        Err(Failure::Inconsistent(Inconsistent::InventoryBinding)),
    );
    drop(acquired);
    std::fs::remove_file(source_directory.join("inventory.json")).unwrap();
    std::fs::remove_dir(&source_directory).unwrap();
}

#[test]
fn blocked_query_expiry_and_unresolved_ownership_do_not_claim_completion() {
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.query_error = Some(Failure::Unavailable(Unavailable::UnresolvedOwnership));
    let mut operation = operation(backend, &policy, "state").unwrap();
    assert_eq!(
        operation.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::UnresolvedOwnership)
    );
    operation.backend.time.monotonic += Duration::from_secs(2);
    assert_eq!(
        operation.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::DeadlineExceeded)
    );
    assert!(operation.pending());
    operation.backend.completed = true;
    operation.backend.query_error = None;
    assert_eq!(
        operation.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::DeadlineExceeded)
    );
    assert!(!operation.pending());
}

#[test]
fn simulated_worker_owns_descriptor_lease_even_if_request_owner_is_dropped() {
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.pending = true;
    let job = Arc::clone(&backend.job);
    let mut operation = operation(backend, &policy, "state").unwrap();
    assert!(operation.poll().is_err());
    let lease = Arc::downgrade(job.lock().unwrap().as_ref().unwrap());
    drop(operation);
    assert!(lease.upgrade().is_some());
    // Only the independently owned simulated worker confirms completion.
    job.lock().unwrap().take();
    assert!(lease.upgrade().is_none());
}

#[test]
fn budgets_and_descriptor_counts_are_measurably_bounded() {
    assert!(ObservationBudget::new(Duration::ZERO).is_err());
    assert!(ObservationBudget::new(MAX_OPERATION_TIME + Duration::from_nanos(1)).is_err());
    let mut value = document();
    value["roles"][7]["role"]["path"] = json!("/synthetic/native/a/b/c/d/e/f/g/state");
    regenerate(&mut value);
    let policy = candidate(&value).unwrap();
    let mut operation = operation(FakeBackend::new(&policy, "state"), &policy, "state").unwrap();
    assert_eq!(operation.descriptor_count(), 12);
    assert!(operation.descriptor_count() <= MAX_DESCRIPTORS);
    assert!(operation.poll().is_ok());
    assert_eq!(operation.descriptor_count(), 0);
    value["roles"][7]["role"]["path"] = json!("/synthetic/native/a/b/c/d/e/f/g/h/state");
    regenerate(&mut value);
    assert!(candidate(&value).is_err());
}

#[test]
fn protocol_rejects_invalid_epoch_and_errors_never_have_observation_fields() {
    let policy = policy();
    let request = binding(&policy, "state");
    let mut inspector = NativeInspector::new();
    let result = inspector.inspect(
        &policy,
        &request,
        ObservationBudget::new(MAX_OPERATION_TIME).unwrap(),
    );
    assert!(result.protocol_response(&"A".repeat(32)).is_err());
    assert!(result.protocol_response("").is_err());
    let wire: Value =
        serde_json::from_slice(&result.protocol_response(&"b".repeat(32)).unwrap()).unwrap();
    for field in [
        "observed_at",
        "byte_limit",
        "quota_configuration_enabled",
        "project_id",
        "error",
    ] {
        assert!(wire.get(field).is_none(), "{field}");
    }
}

#[test]
fn single_active_operation_refuses_concurrency_and_has_no_retry_queue() {
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.pending = true;
    let mut controller = crate::observation::Controller { active: None };
    let request = binding(&policy, "state");
    let budget = ObservationBudget::new(MAX_OPERATION_TIME).unwrap();
    let blocked = controller.inspect(&policy, &request, budget, backend);
    assert!(matches!(
        blocked.outcome(),
        Outcome::Failed(Failure::Unavailable(Unavailable::Blocked))
    ));
    let busy = controller.inspect(
        &policy,
        &request,
        budget,
        FakeBackend::new(&policy, "state"),
    );
    assert!(matches!(
        busy.outcome(),
        Outcome::Failed(Failure::Unavailable(Unavailable::Busy))
    ));
    assert_eq!(controller.active.as_ref().unwrap().backend.query_calls, 1);
    controller.cancel();
    let cancelled = controller.poll().unwrap();
    assert!(matches!(
        cancelled.outcome(),
        Outcome::Failed(Failure::Unavailable(Unavailable::Cancelled))
    ));
    assert!(controller.active.is_some());
    controller.active.as_mut().unwrap().backend.completed = true;
    let cancelled = controller.poll().unwrap();
    assert!(matches!(
        cancelled.outcome(),
        Outcome::Failed(Failure::Unavailable(Unavailable::Cancelled))
    ));
    assert!(controller.active.is_none());
    assert!(controller.poll().is_none());
}

#[test]
fn bounded_inventory_role_and_existing_file_counts_are_enforced() {
    let mut excess_inodes = document();
    excess_inodes["roles"][0]["role"]["byte_limit"] = json!(1024);
    let mut extra = excess_inodes["roles"][0].clone();
    extra["role"]["role_id"] = json!("extra-inodes");
    extra["role"]["path"] = json!("/synthetic/native/extra-inodes");
    extra["role"]["project_id"] = json!(5000);
    excess_inodes["roles"].as_array_mut().unwrap().push(extra);
    regenerate(&mut excess_inodes);
    assert_eq!(
        candidate(&excess_inodes).err(),
        Some(Failure::Unavailable(Unavailable::InventoryInvalid))
    );
    let mut value = document();
    for index in 10..32 {
        let mut role = value["roles"][0].clone();
        role["role"]["role_id"] = json!(format!("extra-{index}"));
        role["role"]["path"] = json!(format!("/synthetic/native/extra-{index}"));
        role["role"]["project_id"] = json!(5000 + index);
        role["role"]["byte_limit"] = json!(1024);
        role["role"]["inode_limit"] = json!(16);
        value["roles"].as_array_mut().unwrap().push(role);
    }
    // Reduce one task entitlement so the additional fixed domains fit the same ceiling.
    value["roles"][0]["role"]["byte_limit"] = json!(1024);
    value["roles"][0]["role"]["inode_limit"] = json!(16);
    regenerate(&mut value);
    assert_eq!(candidate(&value).unwrap().role_count(), MAX_ROLES);
    let extra = value["roles"][31].clone();
    value["roles"].as_array_mut().unwrap().push(extra);
    regenerate(&mut value);
    assert!(candidate(&value).is_err());
    let mut value = document();
    value["roles"][7]["existing_files"] = json!([
        {"name":"a","purpose":"sqlite_db","mode":384},
        {"name":"b","purpose":"sqlite_wal","mode":384},
        {"name":"c","purpose":"sqlite_shm","mode":384},
        {"name":"d","purpose":"log","mode":384},
        {"name":"e","purpose":"log","mode":384},
    ]);
    assert!(candidate(&value).is_err());
}

#[test]
fn unknown_namespace_and_descriptor_loss_after_query_are_not_success() {
    fail_node(
        |node| node.filesystem.as_mut().unwrap().namespace.inode = 0,
        "state",
        Failure::Unavailable(Unavailable::NamespaceUnavailable),
        0,
    );
    fail_query(
        |quota| {
            quota.after.filesystem.namespace.inode = 0;
            quota.before = quota.after.clone();
        },
        Failure::Unavailable(Unavailable::NamespaceUnavailable),
    );
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.pending = true;
    let mut operation = operation(backend, &policy, "state").unwrap();
    assert!(operation.poll().is_err());
    operation.backend.completed = true;
    operation.backend.node_error = Some(Failure::Unavailable(Unavailable::DescriptorUnavailable));
    assert_eq!(
        operation.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::DescriptorUnavailable)
    );
    assert!(!operation.pending());
}

#[test]
fn aliasing_pinned_inodes_and_wrong_log_project_are_rejected() {
    fail_node(
        |node| node.inode = 100,
        "state",
        Failure::Inconsistent(Inconsistent::AliasedDomain),
        0,
    );
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "logs");
    backend.node_target = Some("broker.log".into());
    backend.node_mutator = Some(|node| node.project.as_mut().unwrap().project_id.0 += 1);
    assert_eq!(
        operation(backend, &policy, "logs").err(),
        Some(Failure::Inconsistent(Inconsistent::ProjectIdentity))
    );
}

#[test]
fn readonly_quota_fields_require_valid_masks_complete_values_and_checked_units() {
    let fields = ReadOnlyQuotaFields {
        valid_mask: 0x0f,
        hard_blocks_1024: Some(1024),
        hard_inodes: Some(128),
        used_bytes: Some(64),
        used_inodes: Some(1),
    };
    assert_eq!(
        fields.validated_counters().unwrap(),
        (
            HardLimits {
                bytes: 1_048_576,
                inodes: 128
            },
            Usage {
                bytes: 64,
                inodes: 1
            },
        )
    );
    for missing_bit in [1, 2, 4, 8] {
        assert_eq!(
            ReadOnlyQuotaFields {
                valid_mask: 0x0f ^ missing_bit,
                ..fields
            }
            .validated_counters(),
            Err(Failure::Unavailable(Unavailable::MissingEvidence)),
        );
    }
    for incomplete in [
        ReadOnlyQuotaFields {
            hard_blocks_1024: None,
            ..fields
        },
        ReadOnlyQuotaFields {
            hard_inodes: None,
            ..fields
        },
        ReadOnlyQuotaFields {
            used_bytes: None,
            ..fields
        },
        ReadOnlyQuotaFields {
            used_inodes: None,
            ..fields
        },
        ReadOnlyQuotaFields {
            valid_mask: 0xff,
            ..fields
        },
    ] {
        assert_eq!(
            incomplete.validated_counters(),
            Err(Failure::Unavailable(Unavailable::MissingEvidence))
        );
    }
    assert_eq!(
        ReadOnlyQuotaFields {
            hard_blocks_1024: Some(u64::MAX),
            ..fields
        }
        .validated_counters(),
        Err(Failure::Unavailable(Unavailable::ArithmeticOverflow)),
    );
    assert!(
        ReadOnlyQuotaFields {
            hard_inodes: Some(0),
            ..fields
        }
        .validated_counters()
        .is_err()
    );
}

#[test]
fn adapter_timeout_and_cancellation_are_not_implicit_kernel_completion() {
    let policy = policy();
    for reason in [Unavailable::DeadlineExceeded, Unavailable::Cancelled] {
        let mut backend = FakeBackend::new(&policy, "state");
        backend.query_error = Some(Failure::Unavailable(reason));
        let job = Arc::clone(&backend.job);
        let mut operation = operation(backend, &policy, "state").unwrap();
        assert_eq!(operation.poll().unwrap_err(), Failure::Unavailable(reason));
        assert!(operation.pending());
        assert!(job.lock().unwrap().is_some());
        assert_eq!(operation.poll().unwrap_err(), Failure::Unavailable(reason));
        assert!(operation.pending());
        operation.backend.completed = true;
        assert_eq!(operation.poll().unwrap_err(), Failure::Unavailable(reason));
        assert!(!operation.pending());
        assert!(job.lock().unwrap().is_none());
    }
}

#[test]
fn unavailable_adapter_refuses_even_with_test_attested_policy_and_owned_descriptors() {
    use crate::evidence::UnavailableBackend;
    use std::os::fd::AsFd;

    let policy = policy();
    let mut fake = FakeBackend::new(&policy, "state");
    let attestation = fake.attest(&policy).unwrap();
    let inventory = TrustedInventory {
        policy,
        attestation,
    };
    let mut unavailable = UnavailableBackend;
    let refused = Failure::Unavailable(Unavailable::NativeBackendUnavailable);
    assert_eq!(unavailable.trusted_root(&inventory).err(), Some(refused));
    assert_eq!(unavailable.node(fake.disposable.file.as_fd()), Err(refused));
    assert_eq!(
        unavailable.root_still_attested(&inventory, fake.disposable.file.as_fd()),
        Err(refused)
    );
    assert_eq!(
        unavailable
            .open_component(fake.disposable.file.as_fd(), "state", RESOLUTION)
            .err(),
        Some(refused)
    );
    let lease = Arc::new(DescriptorSet {
        nodes: vec![],
        role_index: 0,
        file_indices: vec![],
        _source_inventory: None,
    });
    assert!(matches!(
        unavailable.read_project_quota(fake.disposable.file.as_fd(), fake.project, lease),
        QueryProgress::Complete(Err(error)) if error == refused
    ));
    assert!(
        matches!(unavailable.poll_quota(), QueryProgress::Complete(Err(error)) if error == refused)
    );
}

#[test]
fn acquisition_consumes_budget_and_clock_regression_or_overflow_is_unavailable() {
    let policy = policy();
    let mut backend = FakeBackend::new(&policy, "state");
    backend.attestation_delay = MAX_OPERATION_TIME;
    assert_eq!(
        operation(backend, &policy, "state").err(),
        Some(Failure::Unavailable(Unavailable::DeadlineExceeded))
    );
    let mut backend = FakeBackend::new(&policy, "state");
    backend.time.monotonic = Duration::MAX;
    assert_eq!(
        operation(backend, &policy, "state").err(),
        Some(Failure::Unavailable(Unavailable::ArithmeticOverflow))
    );
    let mut operation = operation(FakeBackend::new(&policy, "state"), &policy, "state").unwrap();
    operation.backend.time.monotonic -= Duration::from_secs(1);
    assert_eq!(
        operation.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::MalformedEvidence)
    );
    assert!(!operation.pending());
    assert_eq!(operation.backend.query_calls, 0);
}

#[test]
fn original_terminal_pending_then_valid_completion_reproduction_is_refused() {
    let policy = policy();
    for reason in [Unavailable::Cancelled, Unavailable::DeadlineExceeded] {
        let mut backend = FakeBackend::new(&policy, "state");
        backend.query_error = Some(Failure::Unavailable(reason));
        let mut request = operation(backend, &policy, "state").unwrap();
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
        request.backend.query_error = None;
        request.backend.completed = true;
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
        assert!(!request.pending());
        assert_eq!(request.descriptor_count(), 0);
    }
}

#[test]
fn terminal_pending_outcomes_survive_all_completion_and_serialization_paths() {
    let policy = policy();
    let request = binding(&policy, "state");
    let budget = ObservationBudget::new(MAX_OPERATION_TIME).unwrap();
    for reason in [Unavailable::Cancelled, Unavailable::DeadlineExceeded] {
        for completion in 0..3 {
            let mut backend = FakeBackend::new(&policy, "state");
            backend.query_error = Some(Failure::Unavailable(reason));
            let job = Arc::clone(&backend.job);
            let mut controller = crate::observation::Controller { active: None };
            let first = controller.inspect(&policy, &request, budget, backend);
            assert!(matches!(first.outcome(), Outcome::Failed(error)
                if *error == Failure::Unavailable(reason)));
            let lease = Arc::downgrade(job.lock().unwrap().as_ref().unwrap());
            let count = controller.active.as_ref().unwrap().descriptor_count();
            for pending_reason in [
                None,
                Some(Failure::Unavailable(Unavailable::UnresolvedOwnership)),
                Some(Failure::Unavailable(Unavailable::Cancelled)),
                Some(Failure::Unavailable(Unavailable::DeadlineExceeded)),
            ] {
                controller.active.as_mut().unwrap().backend.query_error = pending_reason;
                for _ in 0..5 {
                    let result = controller.poll().unwrap();
                    assert!(matches!(result.outcome(), Outcome::Failed(error)
                        if *error == Failure::Unavailable(reason)));
                    let wire: Value =
                        serde_json::from_slice(&result.protocol_response(&"c".repeat(32)).unwrap())
                            .unwrap();
                    assert_eq!(wire["status"], "unavailable");
                    assert_eq!(wire["error_code"], Failure::Unavailable(reason).code());
                    assert_eq!(wire.as_object().unwrap().len(), 8);
                    assert!(wire.get("observed_at").is_none());
                    assert!(controller.active.as_ref().unwrap().pending());
                    assert_eq!(
                        controller.active.as_ref().unwrap().descriptor_count(),
                        count
                    );
                    assert!(lease.upgrade().is_some());
                    controller.cancel();
                }
            }
            let busy = controller.inspect(
                &policy,
                &request,
                budget,
                FakeBackend::new(&policy, "state"),
            );
            assert!(matches!(
                busy.outcome(),
                Outcome::Failed(Failure::Unavailable(Unavailable::Busy))
            ));
            let backend = &mut controller.active.as_mut().unwrap().backend;
            backend.query_error = match completion {
                1 => Some(Failure::Unavailable(Unavailable::PermissionDenied)),
                _ => None,
            };
            if completion == 2 {
                backend.quota.after.project_id.0 += 1;
                backend.quota.after.usage.bytes += 1;
            }
            backend.completed = true;
            let result = controller.poll().unwrap();
            assert!(matches!(result.outcome(), Outcome::Failed(error)
                if *error == Failure::Unavailable(reason)));
            assert!(controller.active.is_none());
            assert!(controller.poll().is_none());
            assert!(job.lock().unwrap().is_none());
            assert!(lease.upgrade().is_none());
            let fresh = controller.inspect(
                &policy,
                &request,
                budget,
                FakeBackend::new(&policy, "state"),
            );
            assert!(matches!(fresh.outcome(), Outcome::Observed(_)));
            assert!(controller.active.is_none());
        }
    }
}

#[test]
fn transient_blocking_and_unresolved_work_remain_recoverable_before_expiry() {
    let policy = policy();
    for reason in [Unavailable::Blocked, Unavailable::UnresolvedOwnership] {
        let mut backend = FakeBackend::new(&policy, "state");
        backend.query_error = Some(Failure::Unavailable(reason));
        let job = Arc::clone(&backend.job);
        let mut request = operation(backend, &policy, "state").unwrap();
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
        let lease = Arc::downgrade(job.lock().unwrap().as_ref().unwrap());
        for _ in 0..20 {
            assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
            assert!(request.pending());
            assert!(lease.upgrade().is_some());
        }
        request.backend.query_error = None;
        request.backend.completed = true;
        assert!(request.poll().is_ok());
        assert!(!request.pending());
        assert!(lease.upgrade().is_none());
        assert!(request.poll().is_err());
    }
}

#[test]
fn first_terminal_reason_wins_at_local_and_adapter_deadline_boundaries() {
    let policy = policy();
    for cancel_before_deadline in [true, false] {
        let mut backend = FakeBackend::new(&policy, "state");
        backend.pending = true;
        let mut request = operation(backend, &policy, "state").unwrap();
        assert!(request.poll().is_err());
        request.backend.time.monotonic += MAX_OPERATION_TIME - Duration::from_nanos(1);
        if !cancel_before_deadline {
            request.backend.time.monotonic += Duration::from_nanos(1);
        }
        request.cancel();
        let reason = if cancel_before_deadline {
            Unavailable::Cancelled
        } else {
            Unavailable::DeadlineExceeded
        };
        request.backend.time.monotonic += Duration::from_nanos(1);
        for _ in 0..20 {
            request.cancel();
            assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
            assert!(request.pending());
        }
        request.backend.completed = true;
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
        assert!(!request.pending());
        request.cancel();
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
    }
    for reason in [Unavailable::Cancelled, Unavailable::DeadlineExceeded] {
        let mut backend = FakeBackend::new(&policy, "state");
        backend.query_error = Some(Failure::Unavailable(reason));
        let mut request = operation(backend, &policy, "state").unwrap();
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
        request.backend.time.monotonic += MAX_OPERATION_TIME;
        request.cancel();
        request.backend.query_error = None;
        request.backend.completed = true;
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
    }
}

#[test]
fn dropping_terminal_owner_preserves_independent_worker_lease_until_finalization() {
    let policy = policy();
    for reason in [Unavailable::Cancelled, Unavailable::DeadlineExceeded] {
        let mut backend = FakeBackend::new(&policy, "state");
        backend.query_error = Some(Failure::Unavailable(reason));
        let job = Arc::clone(&backend.job);
        let mut request = operation(backend, &policy, "state").unwrap();
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
        let lease = Arc::downgrade(job.lock().unwrap().as_ref().unwrap());
        request.cancel();
        request.cancel();
        drop(request);
        assert!(lease.upgrade().is_some());
        assert_eq!(lease.strong_count(), 1);
        // The independently owned simulated worker confirms finalization.
        job.lock().unwrap().take();
        assert!(lease.upgrade().is_none());
    }
}

#[test]
fn nontransient_pending_failure_and_clock_loss_cannot_restore_success() {
    let policy = policy();
    for reason in [
        Unavailable::MissingEvidence,
        Unavailable::DescriptorUnavailable,
    ] {
        let mut backend = FakeBackend::new(&policy, "state");
        backend.pending = true;
        let mut request = operation(backend, &policy, "state").unwrap();
        assert!(request.poll().is_err());
        request.backend.query_error = Some(Failure::Unavailable(reason));
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
        assert!(request.pending());
        request.backend.query_error = None;
        request.backend.completed = true;
        assert_eq!(request.poll().unwrap_err(), Failure::Unavailable(reason));
        assert!(!request.pending());
    }
    let mut backend = FakeBackend::new(&policy, "state");
    backend.pending = true;
    let mut request = operation(backend, &policy, "state").unwrap();
    assert!(request.poll().is_err());
    request.backend.time.monotonic -= Duration::from_secs(1);
    assert_eq!(
        request.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::MalformedEvidence)
    );
    assert!(request.pending());
    request.backend.time.monotonic += Duration::from_secs(1);
    request.backend.completed = true;
    assert_eq!(
        request.poll().unwrap_err(),
        Failure::Unavailable(Unavailable::MalformedEvidence)
    );
    assert!(!request.pending());
}
