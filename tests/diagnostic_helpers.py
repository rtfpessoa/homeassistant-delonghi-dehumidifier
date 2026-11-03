"""Enable optional diagnostics through Home Assistant's entity registry."""

from homeassistant.helpers import entity_registry as er


async def enable_diagnostics(hass, entry, *entity_ids):
    """Exercise the user opt-in, then load the updated registry settings."""
    registry = er.async_get(hass)
    for entity_id in entity_ids:
        assert (
            registry.async_get(entity_id).disabled_by
            == er.RegistryEntryDisabler.INTEGRATION
        )
        assert hass.states.get(entity_id) is None
        registry.async_update_entity(entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
