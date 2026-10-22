"""Coordinate commands that affect the humidifier's cloud snapshot."""

import asyncio
from collections.abc import Awaitable, Callable


class HumidifierCommands:
    """Serialize commands and notify listeners when observations need refresh."""

    def __init__(self, invalidate_properties: Callable[[], None]) -> None:
        """Initialize the command state for one integration entry."""
        self._lock = asyncio.Lock()
        self._generation = 0
        self._invalidate_properties = invalidate_properties
        self._refresh_listeners: set[Callable[[], None]] = set()

    @property
    def generation(self) -> int:
        """Return the generation observed by humidifier polls."""
        return self._generation

    def subscribe_refresh(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to refresh requests and return an unsubscribe callback."""
        self._refresh_listeners.add(listener)
        return lambda: self._refresh_listeners.discard(listener)

    async def run(
        self,
        send: Callable[[], Awaitable[dict]],
        *,
        guard: Callable[[], Awaitable[None]] | None = None,
        refresh_after_success: bool = False,
    ) -> None:
        """Run an optional guard and a command under the same lock.

        The guard runs before the command attempt. A rejected or canceled guard
        does not invalidate observations on behalf of a write that never started.
        """
        async with self._lock:
            if guard is not None:
                await guard()
            accepted = False
            try:
                await send()
                accepted = True
            finally:
                self._generation += 1
                self._invalidate_properties()
                if not accepted or refresh_after_success:
                    for listener in tuple(self._refresh_listeners):
                        listener()
