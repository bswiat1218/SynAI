use std::sync::Arc;

use crate::{Failure, Inconsistent, NamespaceId, Unavailable};

/// Exact instance, not a PID-only signal target. Values are not acquisition proof.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct WorkerIdentity {
    pub pid: u32,
    pub start_ticks: u64,
    pub generation: u64,
    pub boot_epoch: [u8; 16],
    pub pid_namespace: NamespaceId,
}

/// Offline ownership contract. No worker is spawned or signalled by this model.
pub(crate) struct WorkerLease<T> {
    identity: WorkerIdentity,
    lease: Option<Arc<T>>,
    exited: bool,
    reaped: bool,
    cleaned: bool,
}

impl<T> WorkerLease<T> {
    pub(crate) fn new(identity: WorkerIdentity, lease: Arc<T>) -> Result<Self, Failure> {
        if identity.pid == 0
            || identity.start_ticks == 0
            || identity.generation == 0
            || identity.boot_epoch == [0; 16]
            || identity.pid_namespace.inode == 0
        {
            return Err(Failure::Unavailable(Unavailable::UnresolvedOwnership));
        }
        Ok(Self {
            identity,
            lease: Some(lease),
            exited: false,
            reaped: false,
            cleaned: false,
        })
    }

    fn exact(&self, identity: WorkerIdentity) -> Result<(), Failure> {
        if identity != self.identity {
            return Err(Failure::Inconsistent(Inconsistent::NodeIdentity));
        }
        Ok(())
    }

    pub(crate) fn exited(&mut self, identity: WorkerIdentity) -> Result<(), Failure> {
        self.exact(identity)?;
        self.exited = true;
        Ok(())
    }

    pub(crate) fn reaped(&mut self, identity: WorkerIdentity) -> Result<(), Failure> {
        self.exact(identity)?;
        if !self.exited {
            return Err(Failure::Unavailable(Unavailable::UnresolvedOwnership));
        }
        self.reaped = true;
        Ok(())
    }

    pub(crate) fn cleaned(&mut self, identity: WorkerIdentity) -> Result<(), Failure> {
        self.exact(identity)?;
        if !self.reaped {
            return Err(Failure::Unavailable(Unavailable::UnresolvedOwnership));
        }
        self.cleaned = true;
        Ok(())
    }

    pub(crate) fn finalize(&mut self) -> Result<(), Failure> {
        if !self.exited || !self.reaped || !self.cleaned {
            return Err(Failure::Unavailable(Unavailable::UnresolvedOwnership));
        }
        self.lease = None;
        Ok(())
    }
}
