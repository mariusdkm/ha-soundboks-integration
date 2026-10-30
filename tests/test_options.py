"""The options: a power switch, a turn-on action and a battery sensor, all optional."""

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soundboks.const import (
    CONF_BATTERY_ENTITY,
    CONF_POWER_SWITCH,
    CONF_TURN_ON_ENTITY,
)

from .conftest import FakeSpeaker, make_entry, setup_entry, setup_present

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

MEDIA_PLAYER = "media_player.soundboks_202047"


async def configure(hass: HomeAssistant, entry: MockConfigEntry, user_input: dict) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result


ALL_OPTIONS = {
    CONF_POWER_SWITCH: "switch.speaker_plug",
    CONF_TURN_ON_ENTITY: "script.speaker_on",
    CONF_BATTERY_ENTITY: "sensor.pack_battery",
}


@pytest.mark.parametrize(
    ("before", "user_input"),
    [
        ({}, ALL_OPTIONS),
        ({}, {CONF_POWER_SWITCH: "switch.speaker_plug"}),
        (ALL_OPTIONS, {}),  # cleared
    ],
)
async def test_options_saved(
    hass: HomeAssistant, fake_speaker: FakeSpeaker, before: dict, user_input: dict
):
    entry = make_entry(**before)
    await setup_entry(hass, entry)
    result = await configure(hass, entry, user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == user_input


async def test_options_change_reloads_without_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_present(hass, mock_entry)
    assert hass.states.get(MEDIA_PLAYER).state == STATE_ON
    await configure(hass, mock_entry, {CONF_TURN_ON_ENTITY: "script.speaker_on"})
    assert mock_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(MEDIA_PLAYER).state == STATE_ON
