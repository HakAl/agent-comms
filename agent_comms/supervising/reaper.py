"""One module-global parent reaper registry per spawning process.

Retains each supervised wrapper ``Popen`` with the identities its terminal-proof
barriers need, reaps on a bounded schedule and records the registered reap.
"""
from __future__ import annotations

import logging
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from . import evidence, paths, process, zdotdir

logger = logging.getLogger("agent_comms.supervisor.reaper")


# Bounded, non-signalling parent-side proof of the registered native child
# process group: the wrapper (server side) already drained it before its HALT
# response, so this poll returns immediately in the normal path. The parent
# never signals the group; a group that has not drained refuses the proof (the
# entry is retained for bounded retry) rather than blocking or force-killing.
_REAP_GROUP_VERIFY_TIMEOUT_SECONDS = 2.0


@dataclass
class _ReaperEntry:
    handle: str
    popen: subprocess.Popen
    db_path: str
    dispatch_id: str
    run_token: str
    run_dir: str | None
    zdotdir: str | None
    # Revision 7 F2: the exact parent-registry association retains, alongside the
    # exact wrapper ``Popen`` above, the identities the terminal-proof barriers
    # need -- the exact control socket, the wrapper process-group id (the wrapper
    # is launched ``start_new_session`` so its group id equals its PID), and the
    # registered native child process-group id (from ``SupervisedSpawn.child_pid``,
    # itself a ``start_new_session`` leader). Identity is never rediscovered by
    # ``ps``/glob/recency/spawn_handle parsing; it is what the adapter registered
    # at READY. ``None`` means an identity was not registered and the matching
    # barrier fails closed rather than being assumed drained.
    control_socket: str | None = None
    wrapper_pgid: int | None = None
    child_pgid: int | None = None
    # Single-owner marker: an entry is CLAIMED atomically (under the registry
    # lock) by exactly one reaping path -- a HALT that owns the exact-wrapper reap,
    # or one reap_ready() caller. A claimed entry is skipped by every other path so
    # one registered wrapper is never processed twice; a failed HALT releases it.
    claimed: bool = False


class ReaperRegistry:
    """One registry + one background thread per spawning process.

    It retains every wrapper ``Popen`` (the adapter's direct children), reaps
    those that have exited, merges exact same-run return code / time into only
    the matching run-token row, and performs idempotent fallback cleanup. It
    never transitions ledger status; the producer cap bounds its size.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _ReaperEntry] = {}
        # Claimed entries displaced by a same-handle re-registration are held here
        # (not in ``_entries``, whose key is now the replacement) so fallback
        # reaping never loses a HALT-claimed wrapper. Reaped/finalized out
        # identity-guarded; only ever holds entries an owner still has to
        # finalize() or release_claim().
        self._displaced: list[_ReaperEntry] = []
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = False

    def register(
        self,
        handle: str,
        popen: subprocess.Popen,
        *,
        db_path: str,
        dispatch_id: str,
        run_token: str,
        run_dir: str | None = None,
        zdotdir: str | None = None,
        control_socket: str | None = None,
        wrapper_pgid: int | None = None,
        child_pgid: int | None = None,
    ) -> None:
        with self._lock:
            prior = self._entries.get(handle)
            if prior is not None and prior.claimed:
                # A HALT owns ``prior``'s exact reap. Reusing the handle must not
                # drop the claimed wrapper on the floor: preserve it as displaced
                # so a later failed HALT/release restores its fallback reaping
                # instead of stranding it, while the replacement takes the slot.
                self._displaced.append(prior)
            self._entries[handle] = _ReaperEntry(
                handle=handle,
                popen=popen,
                db_path=db_path,
                dispatch_id=dispatch_id,
                run_token=run_token,
                run_dir=run_dir,
                zdotdir=zdotdir,
                control_socket=control_socket,
                wrapper_pgid=wrapper_pgid,
                child_pgid=child_pgid,
            )
            self._ensure_thread_locked()
        self._wake.set()

    def discard(self, handle: str) -> _ReaperEntry | None:
        with self._lock:
            return self._entries.pop(handle, None)

    def find_by_run_token(self, run_token: str) -> _ReaperEntry | None:
        """The retained entry (wrapper ``Popen`` + identity) for a run token.

        Run tokens are unique per dispatch, so this yields the single exact
        registered wrapper the authenticated HALT client reaps for barrier 3.
        Returns ``None`` when no such wrapper is registered (e.g. it already
        exited and was reaped/discarded).
        """
        with self._lock:
            for entry in self._entries.values():
                if entry.run_token == run_token:
                    return entry
        return None

    def pending(self) -> int:
        with self._lock:
            return len(self._entries)

    def claim_by_run_token(self, run_token: str) -> _ReaperEntry | None:
        """Atomically claim sole ownership of the single entry for a run token.

        Returns the claimed entry, or ``None`` when no still-registered, unclaimed
        entry matches. A claimed entry is skipped by ``reap_ready()`` (and by a
        second claim), so the authenticated HALT that owns the exact-wrapper reap
        and the background reaper never both process one registered wrapper. The
        claimer must finalize (:meth:`finalize_claimed`) or release
        (:meth:`release_claim`) it.
        """
        with self._lock:
            for entry in self._entries.values():
                if entry.run_token == run_token:
                    if entry.claimed:
                        return None
                    entry.claimed = True
                    return entry
        return None

    def release_claim(self, entry: _ReaperEntry) -> None:
        """Release a claim so fallback background reaping can still own the entry.

        Used when a HALT does not confirm: the exact wrapper must remain reapable
        by the idempotent fallback rather than being stranded owned-but-unreaped.
        """
        with self._lock:
            entry.claimed = False

    def _reap_barriers_verified(self, entry: _ReaperEntry, *, clean: bool) -> bool:
        """Freshly prove EVERY physical reap barrier for ``entry``.

        In order: the exact wrapper ``Popen`` is reaped; its exact wrapper
        process group is absent (the wrapper is a ``start_new_session`` leader,
        so its group id is its PID -- a registered ``wrapper_pgid`` is used when
        present, else the exact ``Popen`` pid); the registered native child
        process group (a ``start_new_session`` leader whose group id is
        ``child_pid``) is empty -- a missing child identity FAILS CLOSED rather
        than being assumed drained; and the exact run directory, control socket,
        and owned ZDOTDIR parent are absent.

        The no-HALT (background) producer passes ``clean=True`` so it
        idempotently removes ONLY its exact registered owned artifacts before
        the absence check; the HALT producer passes ``clean=False`` because the
        server-side HALT already cleaned and the parent only VERIFIES. Either
        way absence is freshly verified here. Unrelated residue is never touched.
        """
        if entry.popen.poll() is None:
            return False
        wrapper_pgid = entry.wrapper_pgid if entry.wrapper_pgid is not None else entry.popen.pid
        if not process._process_group_gone(wrapper_pgid):
            return False
        if entry.child_pgid is None:
            return False
        if not process.process_group_empty(entry.child_pgid, timeout=_REAP_GROUP_VERIFY_TIMEOUT_SECONDS):
            return False
        if clean:
            if entry.run_dir is not None:
                paths.cleanup_run_dir(Path(entry.run_dir))
            if entry.zdotdir:
                zdotdir._cleanup_zdotdir(Path(entry.zdotdir))
        return self._owned_artifacts_absent(entry)

    def _owned_artifacts_absent(self, entry: _ReaperEntry) -> bool:
        """Whether the exact registered run directory, control socket, and owned
        ZDOTDIR parent are all absent. A path that cannot be ruled out counts as
        present (fail closed). An unregistered (``None``) identity is nothing
        owned to leak and does not by itself fail the barrier."""
        if entry.run_dir is not None and paths._path_present(Path(entry.run_dir)):
            return False
        if entry.control_socket and paths._path_present(Path(entry.control_socket)):
            return False
        zparent = zdotdir._owned_zdotdir_parent(entry.zdotdir)
        if zparent is not None and paths._path_present(zparent):
            return False
        return True

    def finalize_claimed(self, entry: _ReaperEntry) -> bool:
        """Prove every remaining HALT-finalize barrier, THEN durably persist the
        complete reap proof, THEN remove the claimed entry. Returns whether a
        durable complete proof now exists.

        The claimed HALT already drove response -> EOF -> the exact ``Popen``
        wait. Before publishing any proof or dropping the only exact-``Popen``
        association, this proves the exact wrapper process group is absent, the
        registered native child process group is empty, and the exact run
        directory, control socket, and owned ZDOTDIR parent are (freshly
        verified) absent -- the server-side HALT already cleaned them; the parent
        only verifies. Only then does it persist the complete
        ``source="halt_finalize"`` proof and drop the entry. Any barrier miss OR
        persistence failure publishes NO proof, RELEASES the claim, and RETAINS
        the exact entry so a later HALT or the background reaper can retry: no
        claimed entry is stranded and the only exact-``Popen`` association is
        never discarded before a durable complete proof exists. Persistence runs
        OUTSIDE the registry lock (a short SQL transaction). Removal stays
        identity-guarded: a same-handle replacement registered since the claim is
        never evicted; a displaced claimed entry is dropped from the holding list
        instead of the live slot.
        """
        if not self._reap_barriers_verified(entry, clean=False):
            with self._lock:
                entry.claimed = False
            return False
        try:
            persisted = evidence.record_registered_wrapper_reap(
                entry.db_path,
                entry.dispatch_id,
                entry.run_token,
                returncode=entry.popen.returncode,
                source=evidence.REAP_SOURCE_HALT_FINALIZE,
                registered_wrapper_reaped=True,
                native_process_group_drained=True,
                owned_artifacts_absent={"run_dir": True, "control_socket": True, "zdotdir_parent": True},
            )
        except sqlite3.Error as exc:
            logger.warning(
                "halt-finalize reap persistence failed for %s: %s", entry.dispatch_id, exc
            )
            persisted = False
        with self._lock:
            if not persisted:
                entry.claimed = False
                return False
            if self._entries.get(entry.handle) is entry:
                del self._entries[entry.handle]
            else:
                self._displaced = [e for e in self._displaced if e is not entry]
            return True

    def reap_ready(self) -> list[str]:
        """Synchronously reap any exited wrappers. Returns reaped handles.

        Each exited entry is CLAIMED atomically under the lock before processing,
        so concurrent ``reap_ready()`` callers (or a HALT that already claimed it)
        never process one registered wrapper twice. Removal is identity-guarded:
        a same-handle replacement registered since the snapshot is never evicted
        by the stale owner. Displaced-but-claimed wrappers (a HALT-claimed entry
        pushed aside by a same-handle re-registration) are scanned here too, so a
        released claim regains fallback reaping.
        """
        with self._lock:
            items = list(self._entries.values()) + list(self._displaced)
        reaped: list[str] = []
        for entry in items:
            returncode = entry.popen.poll()
            if returncode is None:
                continue
            with self._lock:
                live = self._entries.get(entry.handle) is entry
                displaced = any(e is entry for e in self._displaced)
                if not live and not displaced:
                    continue  # removed or replaced since the snapshot
                if entry.claimed:
                    continue  # another owner (HALT or a concurrent reap) has it
                entry.claimed = True
            try:
                persisted = self._reap_entry(entry, returncode)
            except Exception as exc:
                # Containment: an unexpected per-entry failure (e.g. an owned
                # run-dir cleanup OSError) publishes no proof and must neither
                # strand this claim nor terminate a reaping caller/loop; the
                # exact association is retained below for bounded retry.
                logger.warning(
                    "unexpected reap failure for %s (handle %s): %s",
                    entry.dispatch_id,
                    entry.handle,
                    exc,
                )
                persisted = False
            with self._lock:
                if not persisted:
                    # Durable evidence does not exist yet: keep the exact Popen
                    # association and release the claim so the same idempotent
                    # persistence is retried on the next pass, never discarded.
                    entry.claimed = False
                    continue
                if self._entries.get(entry.handle) is entry:
                    del self._entries[entry.handle]
                else:
                    self._displaced = [e for e in self._displaced if e is not entry]
            reaped.append(entry.handle)
        return reaped

    def _reap_entry(self, entry: _ReaperEntry, returncode: int | None) -> bool:
        """No-HALT background producer for one exited registered wrapper.

        Revision 7 F2 order: ``reap_ready`` has observed the exact wrapper
        exited; this proves the wrapper and registered native child groups
        drained, then IDEMPOTENTLY CLEANS only the exact registered run
        directory / control socket / ZDOTDIR parent and FRESHLY VERIFIES their
        absence (all via :meth:`_reap_barriers_verified` with ``clean=True``),
        and ONLY THEN persists the complete ``source="background_reap"`` proof
        through the same identity-bound primitive the HALT finalize uses.
        ``reap_ready`` drops the entry only after this returns ``True``. A group,
        cleanup, absence, or persistence miss publishes NO proof and returns
        ``False`` so the exact association is retained for bounded retry;
        unrelated residue is never touched.
        """
        if not self._reap_barriers_verified(entry, clean=True):
            return False
        try:
            return evidence.record_registered_wrapper_reap(
                entry.db_path,
                entry.dispatch_id,
                entry.run_token,
                returncode=returncode,
                source=evidence.REAP_SOURCE_BACKGROUND_REAP,
                registered_wrapper_reaped=True,
                native_process_group_drained=True,
                owned_artifacts_absent={"run_dir": True, "control_socket": True, "zdotdir_parent": True},
            )
        except sqlite3.Error as exc:
            logger.warning(
                "background reap persistence failed for %s: %s", entry.dispatch_id, exc
            )
            return False

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(
            target=self._loop, name="agent-comms-reaper", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop:
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            # Recheck after every wake: the wake stop() delivers must never
            # trigger one final post-stop reap.
            if self._stop:
                break
            self.reap_ready()

    def stop(self) -> None:
        """Signal the owned thread and synchronously join it to quiescence.

        Returns only after the thread has exited: in-flight entry processing has
        completed and no post-stop background reap can run (the loop rechecks
        the stop flag after the wake this delivers). Entries stay registered for
        a later synchronous ``reap_ready()`` or a restarted thread."""
        with self._lock:
            self._stop = True
            thread = self._thread
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join()


_REGISTRY: ReaperRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def reaper_registry() -> ReaperRegistry:
    """The one registry for this spawning process."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = ReaperRegistry()
        return _REGISTRY
