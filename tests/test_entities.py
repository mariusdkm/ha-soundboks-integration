"""Sound profile, stereo role, active inputs and reboot."""

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.select import (
    ATTR_OPTION,
    SERVICE_SELECT_OPTION,
)
from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soundboks.protocol import INPUTS_UUID, POWER_UUID, PROFILE_UUID, STEREO_UUID

from .conftest import FakeSpeaker, advertise, setup_entry

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

PROFILE = "select.soundboks_202047_sound_profile"
STEREO = "select.soundboks_202047_stereo_role"
INPUTS = "sensor.soundboks_202047_active_inputs"
REBOOT = "button.soundboks_202047_reboot"


async def select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option},
        blocking=True,
    )


async def test_profile_select(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    assert hass.states.get(PROFILE).state == "power"
    await select(hass, PROFILE, "indoor")
    assert (PROFILE_UUID, bytes([2, 51, 51, 51, 51, 51, 51])) in fake_speaker.writes
    assert hass.states.get(PROFILE).state == "indoor"
    await mock_entry.runtime_data.client.disconnect()


async def test_profile_select_shows_custom(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    fake_speaker.values[PROFILE_UUID] = bytes([3, 1, 2, 3, 4, 5, 6])
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    assert hass.states.get(PROFILE).state == "custom"


async def test_profile_select_custom_keeps_stored_bands(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    fake_speaker.values[PROFILE_UUID] = bytes([0, 1, 26, 51, 76, 101, 51])
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    await select(hass, PROFILE, "custom")
    assert (PROFILE_UUID, bytes([3, 1, 26, 51, 76, 101, 51])) in fake_speaker.writes
    assert hass.states.get(PROFILE).state == "custom"


async def test_stereo_select(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    assert hass.states.get(STEREO).state == "mono"
    await select(hass, STEREO, "left")
    assert (STEREO_UUID, b"\x01") in fake_speaker.writes
    assert hass.states.get(STEREO).state == "left"
    await mock_entry.runtime_data.client.disconnect()


@pytest.mark.parametrize(("bits", "expected"), [(4 | 8, "AUX, Bluetooth"), (0, "None")])
async def test_active_inputs_sensor(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    bits: int,
    expected: str,
):
    fake_speaker.values[INPUTS_UUID] = bytes([0, bits])
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    assert hass.states.get(INPUTS).state == expected


async def test_reboot_button(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: REBOOT}, blocking=True
    )
    assert [data for uuid, data in fake_speaker.writes if uuid == POWER_UUID] == [
        b"\x01\x01",
        b"\x00",
    ]


async def test_extras_unavailable_when_absent(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    for entity_id in (PROFILE, STEREO, INPUTS, REBOOT):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
