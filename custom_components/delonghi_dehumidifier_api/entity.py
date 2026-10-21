"""Shared authentication availability for DeLonghi entities."""

import logging

from homeassistant.helpers.entity import Entity

from .client import APIClient, InvalidCredentialsError


class AuthAvailabilityEntity(Entity):
    """Expose confirmed credential rejection on all entities immediately."""

    client: APIClient
    _poll_generation: int

    def _poll_failed(
        self, generation: int, err: Exception, logger: logging.Logger
    ) -> None:
        """Mark only the newest failed poll unavailable without exposing its details."""
        if generation != self._poll_generation:
            return
        if self.available and not isinstance(err, InvalidCredentialsError):
            logger.warning("Unable to update %s (%s)", self.name, type(err).__name__)
        self._attr_available = False

    @property
    def available(self) -> bool:
        """Keep other platform-specific availability checks."""
        return not self.client.invalid_credentials and super().available

    async def async_added_to_hass(self) -> None:
        """Update the entity state when the shared client rejects credentials."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.client.subscribe_auth_failure(self.async_write_ha_state)
        )
