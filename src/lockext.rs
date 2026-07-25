//! Poison-tolerant mutex locking.
//!
//! Every shared-state mutex in the daemon used to be taken with
//! `.lock().unwrap()`. That turns a panic in *any one* thread into a
//! permanent outage: `Mutex` marks itself poisoned when a holder
//! panics, and every later `.lock().unwrap()` — including the mixer's
//! event loop and every socket handler — then panics too. One bad
//! client request could wedge the whole daemon.
//!
//! We'd rather degrade than die. The state behind these locks is
//! audio/EQ bookkeeping that gets re-derived from the device and
//! PipeWire on the next poll tick, so continuing with possibly-stale
//! state is strictly better for the user than the daemon falling over
//! and taking their mixer with it.
//!
//! `lock_or_recover()` therefore recovers the guard from a poisoned
//! mutex via `into_inner()` instead of panicking.

use std::sync::{Mutex, MutexGuard};

pub trait LockExt<T> {
    /// Lock, recovering the guard if the mutex was poisoned by a
    /// panicking holder.
    fn lock_or_recover(&self) -> MutexGuard<'_, T>;
}

impl<T> LockExt<T> for Mutex<T> {
    fn lock_or_recover(&self) -> MutexGuard<'_, T> {
        self.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}
