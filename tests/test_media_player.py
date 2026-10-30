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
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.soundboks.const import CONF_TURN_ON_ENTITY, PRESENCE_GRACE
from custom_components.soundboks.coordinator import SoundboksCoordinator
from custom_components.soundboks.protocol import INPUTS_UUID, POWER_UUID, VOLUME_UUID

from .conftest import (
    PLAYER,
    FakeSpeaker,
    advertise,
    call_player,
    go_unavailable,
    make_entry,
    setup_entry,
    setup_present,
    tick,
)

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


@pytest.mark.parametrize("entity_id", [PLAYER, "binary_sensor.soundboks_202047_connection"])
async def test_state_follows_presence(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
    entity_id: str,
):
    """On while it advertises; turned off at its knob, off once the grace window ends."""
    await setup_entry(hass, mock_entry)
    assert hass.states.get(entity_id).state == STATE_OFF
    await advertise(hass, mock_entry)
    assert hass.states.get(entity_id).state == STATE_ON
    go_unavailable(hass, mock_entry)
    await tick(hass, freezer, PRESENCE_GRACE - timedelta(minutes=1))
    assert hass.states.get(entity_id).state == STATE_ON
    await tick(hass, freezer, timedelta(minutes=2))
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_volume_and_source_shown(hass: HomeAssistant, coordinator: SoundboksCoordinator):
    attributes = hass.states.get(PLAYER).attributes
    assert attributes[ATTR_MEDIA_VOLUME_LEVEL] == pytest.approx(45 / 255)
    assert attributes[ATTR_INPUT_SOURCE] == "Bluetooth"


async def test_source_lists_every_active_input(hass: HomeAssistant, fake_speaker: FakeSpeaker):
    fake_speaker.values[INPUTS_UUID] = bytes([0, 1 | 4 | 8])
    await setup_present(hass, make_entry())
    assert hass.states.get(PLAYER).attributes[ATTR_INPUT_SOURCE] == "Channel 1, AUX, Bluetooth"


@pytest.mark.parametrize(
    ("service", "data", "raw"),
    [
        (SERVICE_VOLUME_SET, {ATTR_MEDIA_VOLUME_LEVEL: 0.5}, 128),
        (SERVICE_VOLUME_UP, {}, 58),  # 45 + 0.05 * 255, rounded
    ],
)
async def test_volume_services(
    hass: HomeAssistant,
    coordinator: SoundboksCoordinator,
    fake_speaker: FakeSpeaker,
    service: str,
    data: dict,
    raw: int,
):
    await call_player(hass, service, **data)
    assert (VOLUME_UUID, bytes([raw])) in fake_speaker.writes
    level = hass.states.get(PLAYER).attributes[ATTR_MEDIA_VOLUME_LEVEL]
    assert level == pytest.approx(raw / 255)


async def test_turn_off(
    hass: HomeAssistant, coordinator: SoundboksCoordinator, fake_speaker: FakeSpeaker
):
    await call_player(hass, SERVICE_TURN_OFF)
    assert (POWER_UUID, b"\x00") in fake_speaker.writes
    assert hass.states.get(PLAYER).state == STATE_OFF


async def test_set_volume_when_unreachable_raises_ha_error(
    hass: HomeAssistant, coordinator: SoundboksCoordinator, fake_speaker: FakeSpeaker
):
    fake_speaker.unreachable = True
    with pytest.raises(HomeAssistantError) as raised:
        await call_player(hass, SERVICE_VOLUME_SET, **{ATTR_MEDIA_VOLUME_LEVEL: 0.5})
    assert raised.value.translation_key == "unreachable"


async def test_command_when_absent_raises_without_connecting(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    with pytest.raises(HomeAssistantError) as raised:
        await call_player(hass, SERVICE_VOLUME_SET, **{ATTR_MEDIA_VOLUME_LEVEL: 0.5})
    assert raised.value.translation_key == "unreachable"
    assert fake_speaker.connect_count == 0


async def test_no_turn_on_without_option(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    features = hass.states.get(PLAYER).attributes[ATTR_SUPPORTED_FEATURES]
    assert not features & MediaPlayerEntityFeature.TURN_ON


@pytest.mark.parametrize(
    ("entity_id", "state", "service"),
    [("script.speaker_on", "off", "turn_on"), ("button.speaker_on", "unknown", "press")],
)
async def test_turn_on_runs_the_chosen_entity(
    hass: HomeAssistant, fake_speaker: FakeSpeaker, entity_id: str, state: str, service: str
):
    await setup_entry(hass, make_entry(**{CONF_TURN_ON_ENTITY: entity_id}))
    hass.states.async_set(entity_id, state)
    calls = async_mock_service(hass, entity_id.split(".")[0], service)
    features = hass.states.get(PLAYER).attributes[ATTR_SUPPORTED_FEATURES]
    assert features & MediaPlayerEntityFeature.TURN_ON
    await call_player(hass, SERVICE_TURN_ON)
    assert [c.data for c in calls] == [{ATTR_ENTITY_ID: entity_id}]
    # Not on until the speaker actually shows up
    assert hass.states.get(PLAYER).state == STATE_OFF


async def test_turn_on_missing_entity_raises(hass: HomeAssistant, fake_speaker: FakeSpeaker):
    await setup_entry(hass, make_entry(**{CONF_TURN_ON_ENTITY: "script.gone"}))
    with pytest.raises(HomeAssistantError) as raised:
        await call_player(hass, SERVICE_TURN_ON)
    assert raised.value.translation_key == "entity_missing"
