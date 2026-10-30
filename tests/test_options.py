"""The options: a turn-on action and a battery sensor, both optional."""

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

from .conftest import FakeSpeaker, advertise, make_entry, setup_entry

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

MEDIA_PLAYER = "media_player.soundboks_202047"


async def configure(hass: HomeAssistant, entry: MockConfigEntry, user_input: dict) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result


async def test_options_set_both(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    result = await configure(
        hass,
        mock_entry,
        {CONF_TURN_ON_ENTITY: "script.speaker_on", CONF_BATTERY_ENTITY: "sensor.pack_battery"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options == {
        CONF_TURN_ON_ENTITY: "script.speaker_on",
        CONF_BATTERY_ENTITY: "sensor.pack_battery",
    }


async def test_options_cleared(hass: HomeAssistant, fake_speaker: FakeSpeaker):
    entry = make_entry(
        **{CONF_TURN_ON_ENTITY: "script.speaker_on", CONF_BATTERY_ENTITY: "sensor.pack_battery"}
    )
    await setup_entry(hass, entry)
    result = await configure(hass, entry, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {}


async def test_options_change_reloads_without_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    assert hass.states.get(MEDIA_PLAYER).state == STATE_ON
    await configure(hass, mock_entry, {CONF_TURN_ON_ENTITY: "script.speaker_on"})
    assert mock_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(MEDIA_PLAYER).state == STATE_ON


async def test_options_set_power_switch(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    result = await configure(hass, mock_entry, {CONF_POWER_SWITCH: "switch.speaker_plug"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options == {CONF_POWER_SWITCH: "switch.speaker_plug"}
