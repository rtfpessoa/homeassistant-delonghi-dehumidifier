"""Interpret the device data used for the tested Real Feel commands."""


def property_value(properties: list[dict], name: str):
    """Return exactly one property value, or None for malformed data."""
    if type(properties) is not list or any(
        type(item) is not dict for item in properties
    ):
        return None
    matches = [item.get("value") for item in properties if item.get("name") == name]
    return matches[0] if len(matches) == 1 else None


def supports_real_feel(properties: list[dict]) -> bool:
    """Match only the model and firmware tested on the appliance."""
    model = property_value(properties, "appliance_model")
    firmware = property_value(properties, "firmware_version")
    return (
        type(model) is str
        and model == "DDSX220"
        and (
            (type(firmware) is str and firmware == "1.0")
            or (type(firmware) is float and firmware == 1.0)
        )
    )
