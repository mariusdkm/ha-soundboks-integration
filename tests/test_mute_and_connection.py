"""Mute (volume 0 and back), sound modes, and the connection sensor."""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_MUTED,
    ATTR_SOUND_MODE,
    ATTR_SOUND_MODE_LIST,
    SERVICE_SELECT_SOUND_MODE,
    SERVICE_VOLUME_MUTE,
)
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.soundboks.const import PRESENCE_GRACE
from custom_components.soundboks.protocol import PROFILE_UUID, VOLUME_UUID

from .conftest import FakeSpeaker, advertise, go_unavailable, setup_entry

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

PLAYER = "media_player.soundboks_202047"
CONNECTION = "binary_sensor.soundboks_202047_connection"


async def call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        MP_DOMAIN, service, {ATTR_ENTITY_ID: PLAYER, **data}, blocking=True
    )


async def mute(hass: HomeAssistant, muted: bool) -> None:
    await call(hass, SERVICE_VOLUME_MUTE, **{ATTR_MEDIA_VOLUME_MUTED: muted})


async def test_mute_then_unmute_restores_volume(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    assert hass.states.get(PLAYER).attributes[ATTR_MEDIA_VOLUME_MUTED] is False
    await mute(hass, True)
    assert fake_speaker.values[VOLUME_UUID] == b"\x00"
    assert hass.states.get(PLAYER).attributes[ATTR_MEDIA_VOLUME_MUTED] is True
    await mute(hass, False)
    assert fake_speaker.values[VOLUME_UUID] == b"\x2d"
    assert hass.states.get(PLAYER).attributes[ATTR_MEDIA_VOLUME_MUTED] is False
    await mock_entry.runtime_data.client.disconnect()


async def test_mute_and_unmute_are_idempotent(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    await mute(hass, False)
    await mute(hass, True)
    await mute(hass, True)
    assert [w for w in fake_speaker.writes if w[0] == VOLUME_UUID] == [(VOLUME_UUID, b"\x00")]
    await mute(hass, False)
    assert fake_speaker.values[VOLUME_UUID] == b"\x2d"
    await mock_entry.runtime_data.client.disconnect()


async def test_unmute_restores_the_knob_volume(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    """The last non-zero volume wins, wherever it came from."""
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    mock_entry.runtime_data.async_handle_volume(100)
    await mute(hass, True)
    await mute(hass, False)
    assert fake_speaker.values[VOLUME_UUID] == bytes([100])
    await mock_entry.runtime_data.client.disconnect()


async def test_unmute_without_earlier_volume_refuses_to_guess(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    """Muted before a restart: HA never saw a volume to go back to."""
    fake_speaker.values[VOLUME_UUID] = b"\x00"
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    assert hass.states.get(PLAYER).attributes[ATTR_MEDIA_VOLUME_MUTED] is True
    with pytest.raises(HomeAssistantError) as raised:
        await mute(hass, False)
    assert raised.value.translation_key == "nothing_to_unmute"
    assert fake_speaker.values[VOLUME_UUID] == b"\x00"


async def test_sound_mode_follows_profile(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    attributes = hass.states.get(PLAYER).attributes
    assert attributes[ATTR_SOUND_MODE] == "Power"
    assert attributes[ATTR_SOUND_MODE_LIST] == ["Power", "Bass+", "Indoor", "Custom"]
    await call(hass, SERVICE_SELECT_SOUND_MODE, **{ATTR_SOUND_MODE: "Indoor"})
    assert fake_speaker.values[PROFILE_UUID][0] == 2
    assert hass.states.get(PLAYER).attributes[ATTR_SOUND_MODE] == "Indoor"
    await mock_entry.runtime_data.client.disconnect()


async def test_unknown_sound_mode_rejected(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    with pytest.raises(ServiceValidationError):
        await call(hass, SERVICE_SELECT_SOUND_MODE, **{ATTR_SOUND_MODE: "Loud"})
    assert fake_speaker.writes == []


async def test_connection_follows_presence(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
):
    await setup_entry(hass, mock_entry)
    assert hass.states.get(CONNECTION).state == STATE_OFF
    await advertise(hass, mock_entry)
    assert hass.states.get(CONNECTION).state == STATE_ON
    go_unavailable(hass, mock_entry)
    freezer.tick(PRESENCE_GRACE + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTION).state == STATE_OFF
