"""Request a shared observation after a burst of device writes."""

import asyncio
from collections.abc import Callable

_REFRESH_DELAY = 4.0


class ActionRefresh:
    """Notify active entities after the cloud has had a moment to settle."""

    def __init__(
        self, invalidate: Callable[[], None], can_refresh: Callable[[], bool]
    ) -> None:
        self._invalidate = invalidate
        self._can_refresh = can_refresh
        self._listeners: set[Callable[[int], None]] = set()
        self._writes = 0
        self._generation = 0
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False

    def subscribe(self, listener: Callable[[int], None]) -> Callable[[], None]:
        """Subscribe an enabled entity until it is removed."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @property
    def write_in_progress(self) -> bool:
        """Return whether another write supersedes a scheduled observation."""
        return self._writes > 0

    @property
    def generation(self) -> int:
        """Identify the most recently started write."""
        return self._generation

    def write_started(self) -> None:
        """Hold a pending observation until all started writes have finished."""
        self._writes += 1
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def write_finished(self) -> None:
        """Observe after successful or ambiguously failed writes, without retrying."""
        self._writes -= 1
        if (
            not self._writes
            and self._listeners
            and not self._closed
            and self._can_refresh()
        ):
            self._timer = asyncio.get_running_loop().call_later(
                _REFRESH_DELAY, self._refresh
            )

    def _refresh(self) -> None:
        self._timer = None
        if self._closed or self._writes or not self._can_refresh():
            return
        self._invalidate()
        generation = self._generation
        for listener in tuple(self._listeners):
            listener(generation)

    def close(self) -> None:
        """Prevent any scheduled work after the integration is unloaded."""
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._listeners.clear()
