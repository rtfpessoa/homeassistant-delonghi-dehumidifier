import aiohttp
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.device_registry import DeviceInfo

from .client import APIClient, TransientCloudError
from .const import DOMAIN


async def fetch_device_info(client: APIClient) -> DeviceInfo:
    try:
        device_name = await client.get_product_name()
        device_model = await client.get_appliance_model()
        firmware_version = await client.get_firmware_version()
        hardware_version = await client.get_hardware_version()
        device_dsn = await client.get_first_device()
    except (aiohttp.ClientError, TimeoutError, TransientCloudError) as err:
        # The cached initial snapshot may expire while HA forwards platforms.
        # Let HA retry a platform whose later read encounters a transient outage.
        raise PlatformNotReady("Unable to contact DeLonghi") from err

    return DeviceInfo(
        identifiers={(DOMAIN, device_dsn)},
        name=(
            device_name
            if device_name == "DeLonghi Dehumidifier"
            else f"{device_name} Dehumidifier"
        ),
        manufacturer="DeLonghi",
        model=device_model,
        sw_version=firmware_version,
        hw_version=hardware_version,
    )
