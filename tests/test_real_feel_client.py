"""Test the Real Feel exit transport independently of any entity."""

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.delonghi_dehumidifier_api.const import (
    REAL_FEEL_DEACTIVATION_PAYLOAD,
)
from tests.test_client_http import make_client


@pytest.mark.parametrize("status", [201, 307])
async def test_transport_sends_one_post_and_does_not_follow_redirects(status):
    """A redirect cannot replay a command or cause a retry."""
    client, session, _ = make_client(status, {})
    if status == 307:
        with pytest.raises(HomeAssistantError, match="307"):
            await client.request_exit_real_feel()
    else:
        await client.request_exit_real_feel()
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/properties/activate_realfeel/datapoints.json")
    assert kwargs["allow_redirects"] is False
    assert kwargs["json"] == {"datapoint": {"value": REAL_FEEL_DEACTIVATION_PAYLOAD}}
