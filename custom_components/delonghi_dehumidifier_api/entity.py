"""Shared authentication availability for DeLonghi entities."""

from homeassistant.helpers.entity import Entity

from .client import APIClient


class AuthAvailabilityEntity(Entity):
    """Expose confirmed credential rejection on all entities immediately."""

    client: APIClient

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
