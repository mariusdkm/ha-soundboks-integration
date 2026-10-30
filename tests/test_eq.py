"""The custom EQ band entities."""

import asyncio

import pytest
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soundboks.protocol import PROFILE_UUID

from .conftest import FakeSpeaker, advertise, setup_entry

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

PROFILE = "select.soundboks_202047_sound_profile"


def band(n: int) -> str:
    return f"number.soundboks_202047_eq_band_{n}"


async def set_band(hass: HomeAssistant, n: int, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: band(n), ATTR_VALUE: value},
        blocking=True,
    )


async def test_bands_show_stored_eq(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    fake_speaker.values[PROFILE_UUID] = bytes([0, 1, 26, 51, 76, 101, 0])
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    assert [hass.states.get(band(n)).state for n in range(1, 7)] == [
        "-10", "-5", "0", "5", "10", "0"
    ]  # fmt: skip
    state = hass.states.get(band(1))
    assert state.attributes["min"] == -10 and state.attributes["max"] == 10
    assert state.attributes["unit_of_measurement"] == "dB"
    assert state.attributes["friendly_name"] == "SOUNDBOKS #202047 EQ band 1"


async def test_moving_a_band_selects_custom_and_keeps_the_others(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    fake_speaker.values[PROFILE_UUID] = bytes([1, 61, 51, 51, 51, 51, 41])
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    await set_band(hass, 2, 4)
    assert fake_speaker.values[PROFILE_UUID] == bytes([3, 61, 71, 51, 51, 51, 41])
    assert hass.states.get(band(2)).state == "4"
    assert hass.states.get(PROFILE).state == "custom"
    await mock_entry.runtime_data.client.disconnect()


async def test_bands_unavailable_while_speaker_away(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    assert hass.states.get(band(1)).state == STATE_UNAVAILABLE


async def test_concurrent_band_changes_both_stick(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    """Each change builds all six bands: the second must start from the first's result."""
    fake_speaker.values[PROFILE_UUID] = bytes([3, 51, 51, 51, 51, 51, 51])
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    await asyncio.gather(coordinator.async_set_eq_band(0, 5), coordinator.async_set_eq_band(1, -3))
    assert fake_speaker.values[PROFILE_UUID] == bytes([3, 76, 36, 51, 51, 51, 51])
    assert coordinator.data.eq == (5, -3, 0, 0, 0, 0)
    await coordinator.client.disconnect()
