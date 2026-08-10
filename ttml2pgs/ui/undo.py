"""
App-wide snapshot undo/redo (Ctrl+Z / Ctrl+Shift+Z).

Every user edit already funnels through a handful of change signals
(cue pane, selected-cue pane, settings pane, sources pane, preferences),
all of which fire AFTER the mutation. The main window therefore keeps a
"shadow" copy per scope — the state as of the last undo boundary — and
on each change signal pushes that pre-image onto the undo stack, then
re-snapshots. Rapid-fire signals (spin drags, slider scrubs) within
``coalesce_ms`` of each other on the same scope merge into ONE step.

Scopes (keys) are plain tuples chosen by the caller:
    ('doc',  session)   the active document (cues, styles, regions)
    ('meta', session)   per-file settings (offset, fps, output, video)
    ('ov',)             the global overrides / profiles

The manager never applies state itself — undo()/redo() hand back the
snapshot and the caller restores it, then calls refresh() so the shadow
matches the restored state.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

Key = Tuple
Entry = Tuple[Key, str, Any]                    # (key, label, snapshot)


class UndoManager:
    def __init__(self, limit: int = 60, coalesce_ms: int = 900,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.coalesce_ms = coalesce_ms
        self._clock = clock
        self._shadow: Dict[Key, Any] = {}
        self._undo: List[Entry] = []
        self._redo: List[Entry] = []
        self._last_key: Optional[Key] = None
        self._last_ms: float = float('-inf')

    # -- shadows -------------------------------------------------------- #
    def ensure(self, key: Key, snap: Callable[[], Any]):
        """Create the pre-image for key if missing (no-op otherwise)."""
        if key not in self._shadow:
            self._shadow[key] = snap()

    def refresh(self, key: Key, snap: Callable[[], Any]):
        """Reset key's pre-image at a known boundary (session activate /
        reload / after applying an undo). Ends any coalescing run."""
        self._shadow[key] = snap()
        if self._last_key == key:
            self._last_key = None

    def prune(self, live_keys):
        """Drop stacks/shadows for keys no longer alive (closed files)."""
        live = set(live_keys)
        self._undo = [e for e in self._undo if e[0] in live]
        self._redo = [e for e in self._redo if e[0] in live]
        self._shadow = {k: v for k, v in self._shadow.items() if k in live}

    # -- recording ------------------------------------------------------ #
    def record(self, key: Key, snap: Callable[[], Any], label: str):
        """Call AFTER a mutation of key's state. Pushes the shadow
        (pre-change state) unless this extends the current burst."""
        now = self._clock() * 1000.0
        burst = (key == self._last_key
                 and now - self._last_ms <= self.coalesce_ms)
        pre = self._shadow.get(key, None)
        if pre is not None and not burst:
            self._undo.append((key, label, pre))
            del self._undo[:-self.limit]
            self._redo.clear()
        # shadow always tracks the state after the latest signal, so the
        # NEXT burst's push is exactly this burst's end state
        self._shadow[key] = snap()
        self._last_key, self._last_ms = key, now

    # -- undo / redo ---------------------------------------------------- #
    def undo(self, snap_current: Callable[[Key], Any]) -> Optional[Entry]:
        """Pop the newest step. The key's CURRENT state goes onto the
        redo stack; the returned snapshot is for the caller to apply
        (then call refresh)."""
        if not self._undo:
            return None
        key, label, pre = self._undo.pop()
        self._redo.append((key, label, snap_current(key)))
        del self._redo[:-self.limit]
        self._last_key = None
        return key, label, pre

    def redo(self, snap_current: Callable[[Key], Any]) -> Optional[Entry]:
        if not self._redo:
            return None
        key, label, post = self._redo.pop()
        self._undo.append((key, label, snap_current(key)))
        del self._undo[:-self.limit]
        self._last_key = None
        return key, label, post

    def undo_label(self) -> Optional[str]:
        return self._undo[-1][1] if self._undo else None

    def redo_label(self) -> Optional[str]:
        return self._redo[-1][1] if self._redo else None
