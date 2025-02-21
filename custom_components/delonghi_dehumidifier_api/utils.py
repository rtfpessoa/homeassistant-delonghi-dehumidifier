from homeassistant.helpers.device_registry import DeviceInfo

from .client import APIClient
from .const import DOMAIN


async def fetch_device_info(client: APIClient) -> DeviceInfo:
    device_name = await client.get_product_name()
    device_model = await client.get_appliance_model()
    firmware_version = await client.get_firmware_version()
    hardware_version = await client.get_hardware_version()
    device_dsn = await client.get_first_device()

    return DeviceInfo(
        identifiers={(DOMAIN, device_dsn)},
        name=f"{device_name} Dehumidifier",
        manufacturer="DeLonghi",
        model=device_model,
        sw_version=firmware_version,
        hw_version=hardware_version,
    )
