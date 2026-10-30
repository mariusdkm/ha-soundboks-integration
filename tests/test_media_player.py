"""The media player: power state, volume, active inputs."""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_MEDIA_VOLUME_LEVEL,
    SERVICE_VOLUME_SET,
    SERVICE_VOLUME_UP,
    MediaPlayerEntityFeature,
)
from homeassistant.components.media_player import (
    DOMAIN as MP_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.soundboks.const import CONF_TURN_ON_ENTITY
from custom_components.soundboks.protocol import INPUTS_UUID, POWER_UUID, VOLUME_UUID

from .conftest import FakeSpeaker, advertise, go_unavailable, make_entry, setup_entry

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

ENTITY_ID = "media_player.soundboks_202047"


async def call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        MP_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY_ID, **data}, blocking=True
    )


async def test_state_follows_presence(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    assert hass.states.get(ENTITY_ID).state == STATE_OFF
    await advertise(hass, mock_entry)
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_MEDIA_VOLUME_LEVEL] == pytest.approx(45 / 255)
    assert state.attributes[ATTR_INPUT_SOURCE] == "Bluetooth"


async def test_source_lists_every_active_input(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    fake_speaker.values[INPUTS_UUID] = bytes([0, 1 | 4 | 8])
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    assert hass.states.get(ENTITY_ID).attributes[ATTR_INPUT_SOURCE] == "Channel 1, AUX, Bluetooth"


async def test_set_volume(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    await call(hass, SERVICE_VOLUME_SET, **{ATTR_MEDIA_VOLUME_LEVEL: 0.5})
    assert (VOLUME_UUID, b"\x80") in fake_speaker.writes
    level = hass.states.get(ENTITY_ID).attributes[ATTR_MEDIA_VOLUME_LEVEL]
    assert level == pytest.approx(128 / 255)
    await mock_entry.runtime_data.client.disconnect()


async def test_volume_up(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    await call(hass, SERVICE_VOLUME_UP)
    assert (VOLUME_UUID, bytes([58])) in fake_speaker.writes  # 45 + 0.05 * 255, rounded
    await mock_entry.runtime_data.client.disconnect()


async def test_turn_off(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    await call(hass, SERVICE_TURN_OFF)
    assert (POWER_UUID, b"\x00") in fake_speaker.writes
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


async def test_set_volume_when_unreachable_raises_ha_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    fake_speaker.unreachable = True
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, SERVICE_VOLUME_SET, **{ATTR_MEDIA_VOLUME_LEVEL: 0.5})
    assert raised.value.translation_key == "unreachable"


async def test_command_when_absent_raises_without_connecting(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, SERVICE_VOLUME_SET, **{ATTR_MEDIA_VOLUME_LEVEL: 0.5})
    assert raised.value.translation_key == "unreachable"
    assert fake_speaker.connect_count == 0


async def test_grace_expiry_shows_off(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
):
    """Turned off at its button: no more adverts, and after the grace window it shows off."""
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    go_unavailable(hass, mock_entry)
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


async def test_no_turn_on_without_option(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    features = hass.states.get(ENTITY_ID).attributes[ATTR_SUPPORTED_FEATURES]
    assert not features & MediaPlayerEntityFeature.TURN_ON


async def test_turn_on_runs_script(hass: HomeAssistant, fake_speaker: FakeSpeaker):
    entry = make_entry(**{CONF_TURN_ON_ENTITY: "script.speaker_on"})
    await setup_entry(hass, entry)
    hass.states.async_set("script.speaker_on", "off")
    calls = async_mock_service(hass, "script", "turn_on")
    features = hass.states.get(ENTITY_ID).attributes[ATTR_SUPPORTED_FEATURES]
    assert features & MediaPlayerEntityFeature.TURN_ON
    await call(hass, SERVICE_TURN_ON)
    assert len(calls) == 1
    assert calls[0].data == {ATTR_ENTITY_ID: "script.speaker_on"}
    # Not on until the speaker actually shows up
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


async def test_turn_on_presses_button(hass: HomeAssistant, fake_speaker: FakeSpeaker):
    entry = make_entry(**{CONF_TURN_ON_ENTITY: "button.speaker_on"})
    await setup_entry(hass, entry)
    hass.states.async_set("button.speaker_on", "unknown")
    calls = async_mock_service(hass, "button", "press")
    await call(hass, SERVICE_TURN_ON)
    assert len(calls) == 1
    assert calls[0].data == {ATTR_ENTITY_ID: "button.speaker_on"}


async def test_turn_on_missing_entity_raises(hass: HomeAssistant, fake_speaker: FakeSpeaker):
    entry = make_entry(**{CONF_TURN_ON_ENTITY: "script.gone"})
    await setup_entry(hass, entry)
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, SERVICE_TURN_ON)
    assert raised.value.translation_key == "entity_missing"
