"""Turning the speaker off and on through a switch in its power line."""

import logging

import pytest
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.soundboks.const import CONF_POWER_SWITCH, CONF_TURN_ON_ENTITY
from custom_components.soundboks.protocol import AUTO_BOOT_ONCE, POWER_OFF, POWER_UUID

from .conftest import FakeSpeaker, advertise, make_entry, setup_entry

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

ENTITY_ID = "media_player.soundboks_202047"
PLUG = "switch.speaker_plug"


async def call(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(MP_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True)


async def setup_with_plug(hass: HomeAssistant, plug_state: str = "on", **options: str):
    entry = make_entry(**{CONF_POWER_SWITCH: PLUG, **options})
    await setup_entry(hass, entry)
    hass.states.async_set(PLUG, plug_state)
    return entry


def power_writes(speaker: FakeSpeaker) -> list[bytes]:
    return [data for uuid, data in speaker.writes if uuid == POWER_UUID]


async def test_turn_off_arms_auto_boot_then_cuts_power(
    hass: HomeAssistant, fake_speaker: FakeSpeaker
):
    fake_speaker.hang_on_auto_boot = True  # as the real speaker does
    entry = await setup_with_plug(hass)
    await advertise(hass, entry)
    plug_off = async_mock_service(hass, "switch", SERVICE_TURN_OFF)
    await call(hass, SERVICE_TURN_OFF)
    # Armed, and not powered off in software: that would restart it right away
    assert power_writes(fake_speaker) == [AUTO_BOOT_ONCE]
    assert POWER_OFF not in power_writes(fake_speaker)
    assert [c.data for c in plug_off] == [{ATTR_ENTITY_ID: PLUG}]
    assert hass.states.get(ENTITY_ID).state == STATE_OFF
    assert entry.runtime_data.client.is_connected is False


async def test_turn_off_unreachable_leaves_power_on(hass: HomeAssistant, fake_speaker: FakeSpeaker):
    """Cutting the power without arming would leave it off until someone turns its knob."""
    entry = await setup_with_plug(hass)
    await advertise(hass, entry)
    fake_speaker.unreachable = True
    plug_off = async_mock_service(hass, "switch", SERVICE_TURN_OFF)
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, SERVICE_TURN_OFF)
    assert raised.value.translation_key == "auto_boot_not_armed"
    assert plug_off == []


async def test_turn_off_when_already_off_just_cuts_power(
    hass: HomeAssistant, fake_speaker: FakeSpeaker
):
    await setup_with_plug(hass)
    plug_off = async_mock_service(hass, "switch", SERVICE_TURN_OFF)
    await call(hass, SERVICE_TURN_OFF)
    assert fake_speaker.connect_count == 0
    assert len(plug_off) == 1


async def test_turn_on_switches_power_on(hass: HomeAssistant, fake_speaker: FakeSpeaker):
    await setup_with_plug(hass, plug_state="off")
    plug_on = async_mock_service(hass, "switch", SERVICE_TURN_ON)
    features = hass.states.get(ENTITY_ID).attributes[ATTR_SUPPORTED_FEATURES]
    assert features & MediaPlayerEntityFeature.TURN_ON
    await call(hass, SERVICE_TURN_ON)
    assert [c.data for c in plug_on] == [{ATTR_ENTITY_ID: PLUG}]
    # Not on until the speaker boots and advertises
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


async def test_power_switch_takes_over_turn_on_entity(
    hass: HomeAssistant, fake_speaker: FakeSpeaker
):
    await setup_with_plug(hass, plug_state="off", **{CONF_TURN_ON_ENTITY: "script.speaker_on"})
    hass.states.async_set("script.speaker_on", "off")
    script = async_mock_service(hass, "script", SERVICE_TURN_ON)
    plug_on = async_mock_service(hass, "switch", SERVICE_TURN_ON)
    await call(hass, SERVICE_TURN_ON)
    assert len(plug_on) == 1
    assert script == []


async def test_turn_on_with_power_already_on_warns(
    hass: HomeAssistant, fake_speaker: FakeSpeaker, caplog: pytest.LogCaptureFixture
):
    """Powered but off: nothing armed auto-boot, so only the knob can start it."""
    await setup_with_plug(hass, plug_state="on")
    async_mock_service(hass, "switch", SERVICE_TURN_ON)
    with caplog.at_level(logging.WARNING):
        await call(hass, SERVICE_TURN_ON)
    assert "turn its knob" in caplog.text


async def test_missing_power_switch_raises(hass: HomeAssistant, fake_speaker: FakeSpeaker):
    await setup_entry(hass, make_entry(**{CONF_POWER_SWITCH: "switch.gone"}))
    for service in (SERVICE_TURN_ON, SERVICE_TURN_OFF):
        with pytest.raises(HomeAssistantError) as raised:
            await call(hass, service)
        assert raised.value.translation_key == "entity_missing"
    assert fake_speaker.connect_count == 0


async def test_turn_off_without_power_switch_powers_off(
    hass: HomeAssistant, fake_speaker: FakeSpeaker
):
    entry = make_entry()
    await setup_entry(hass, entry)
    await advertise(hass, entry)
    await call(hass, SERVICE_TURN_OFF)
    assert power_writes(fake_speaker) == [POWER_OFF]


async def test_refused_arm_leaves_power_on(hass: HomeAssistant, fake_speaker: FakeSpeaker):
    entry = await setup_with_plug(hass)
    await advertise(hass, entry)
    fake_speaker.failing_writes = {POWER_UUID}
    plug_off = async_mock_service(hass, "switch", SERVICE_TURN_OFF)
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, SERVICE_TURN_OFF)
    assert raised.value.translation_key == "auto_boot_not_armed"
    assert plug_off == []
