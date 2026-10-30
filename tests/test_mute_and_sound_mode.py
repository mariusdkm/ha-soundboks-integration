"""Mute (volume 0 and back) and sound modes."""

import pytest
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_MUTED,
    ATTR_SOUND_MODE,
    ATTR_SOUND_MODE_LIST,
    SERVICE_SELECT_SOUND_MODE,
    SERVICE_VOLUME_MUTE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.soundboks.coordinator import SoundboksCoordinator
from custom_components.soundboks.protocol import PROFILE_UUID, VOLUME_UUID

from .conftest import PLAYER, FakeSpeaker, call_player, make_entry, setup_present

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


async def mute(hass: HomeAssistant, muted: bool) -> None:
    await call_player(hass, SERVICE_VOLUME_MUTE, **{ATTR_MEDIA_VOLUME_MUTED: muted})


def muted(hass: HomeAssistant) -> bool:
    return hass.states.get(PLAYER).attributes[ATTR_MEDIA_VOLUME_MUTED]


async def test_mute_then_unmute_restores_volume(
    hass: HomeAssistant, coordinator: SoundboksCoordinator, fake_speaker: FakeSpeaker
):
    assert muted(hass) is False
    await mute(hass, True)
    assert fake_speaker.values[VOLUME_UUID] == b"\x00"
    assert muted(hass) is True
    await mute(hass, False)
    assert fake_speaker.values[VOLUME_UUID] == b"\x2d"
    assert muted(hass) is False


async def test_mute_and_unmute_are_idempotent(
    hass: HomeAssistant, coordinator: SoundboksCoordinator, fake_speaker: FakeSpeaker
):
    await mute(hass, False)
    await mute(hass, True)
    await mute(hass, True)
    assert [w for w in fake_speaker.writes if w[0] == VOLUME_UUID] == [(VOLUME_UUID, b"\x00")]
    await mute(hass, False)
    assert fake_speaker.values[VOLUME_UUID] == b"\x2d"


async def test_unmute_restores_the_knob_volume(
    hass: HomeAssistant, coordinator: SoundboksCoordinator, fake_speaker: FakeSpeaker
):
    """The last non-zero volume wins, wherever it came from."""
    coordinator.async_handle_volume(100)
    await mute(hass, True)
    await mute(hass, False)
    assert fake_speaker.values[VOLUME_UUID] == bytes([100])


async def test_unmute_without_earlier_volume_refuses_to_guess(
    hass: HomeAssistant, fake_speaker: FakeSpeaker
):
    """Muted before a restart: HA never saw a volume to go back to."""
    fake_speaker.values[VOLUME_UUID] = b"\x00"
    await setup_present(hass, make_entry())
    assert muted(hass) is True
    with pytest.raises(HomeAssistantError) as raised:
        await mute(hass, False)
    assert raised.value.translation_key == "nothing_to_unmute"
    assert fake_speaker.values[VOLUME_UUID] == b"\x00"


async def test_sound_mode_follows_profile(
    hass: HomeAssistant, coordinator: SoundboksCoordinator, fake_speaker: FakeSpeaker
):
    attributes = hass.states.get(PLAYER).attributes
    assert attributes[ATTR_SOUND_MODE] == "Power"
    assert attributes[ATTR_SOUND_MODE_LIST] == ["Power", "Bass+", "Indoor", "Custom"]
    await call_player(hass, SERVICE_SELECT_SOUND_MODE, **{ATTR_SOUND_MODE: "Indoor"})
    assert fake_speaker.values[PROFILE_UUID][0] == 2
    assert hass.states.get(PLAYER).attributes[ATTR_SOUND_MODE] == "Indoor"


async def test_unknown_sound_mode_rejected(
    hass: HomeAssistant, coordinator: SoundboksCoordinator, fake_speaker: FakeSpeaker
):
    with pytest.raises(ServiceValidationError):
        await call_player(hass, SERVICE_SELECT_SOUND_MODE, **{ATTR_SOUND_MODE: "Loud"})
    assert fake_speaker.writes == []
