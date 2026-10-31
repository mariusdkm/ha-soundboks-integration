"""Staying connected (idle timeout 0): one link per power-on, presence while it holds."""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.bluetooth import async_clear_advertisement_history
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soundboks.client import SoundboksClient
from custom_components.soundboks.const import CONF_IDLE_TIMEOUT, PRESENCE_GRACE, RECONNECT_DELAY
from custom_components.soundboks.coordinator import SoundboksCoordinator

from .conftest import (
    ADDRESS,
    FakeSpeaker,
    advertise,
    go_unavailable,
    make_ble_device,
    make_entry,
    setup_present,
    tick,
)


async def test_client_keeps_the_link_and_reports_only_drops(fake_speaker: FakeSpeaker):
    lost: list[None] = []
    client = SoundboksClient(
        make_ble_device(), None, idle_timeout=None, on_link_lost=lambda: lost.append(None)
    )
    await client.read_state()
    await client.set_volume(10)
    await client.read_state()
    assert client.is_connected is True
    assert fake_speaker.connect_count == 1
    assert fake_speaker.client is not None
    fake_speaker.client.drop()  # the speaker went away
    assert lost == [None]
    await client.read_state()  # connects again, and keeps that link too
    await client.disconnect()  # our own hang-up isn't a loss
    assert lost == [None]
    assert fake_speaker.connect_count == 2


# The coordinator, through a config entry staying connected

needs_bluetooth = pytest.mark.usefixtures("enable_bluetooth")


@pytest.fixture
def keep_entry() -> MockConfigEntry:
    return make_entry(**{CONF_IDLE_TIMEOUT: 0})


@pytest.fixture
async def keeping(
    hass: HomeAssistant,
    keep_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
) -> SoundboksCoordinator:
    coordinator = await setup_present(hass, keep_entry)
    await hass.async_block_till_done()
    assert coordinator.client.is_connected
    return coordinator


def drop_link(fake_speaker: FakeSpeaker) -> None:
    assert fake_speaker.client is not None
    fake_speaker.client.drop()


@needs_bluetooth
async def test_held_link_is_reused_and_keeps_it_present(
    hass: HomeAssistant,
    keep_entry: MockConfigEntry,
    keeping: SoundboksCoordinator,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
):
    connects = fake_speaker.connect_count
    go_unavailable(hass, keep_entry)  # it stops advertising because we hold the link
    # One jump past the grace: with polls slower than the grace, none would end it first
    await tick(hass, freezer, PRESENCE_GRACE + timedelta(minutes=1))
    assert keeping.present is True
    for _ in range(2):
        await tick(hass, freezer, timedelta(minutes=3, seconds=5))
    assert fake_speaker.connect_count == connects
    assert sum(data == b"gla" for _uuid, data in fake_speaker.writes) >= 4  # setup + 3 polls


@needs_bluetooth
@pytest.mark.parametrize(
    "callback_skipped",
    [
        False,  # the link was held long: HA forgot the advert, and passes the next one on
        True,  # identical to the advert HA still holds: only the reconnect proves it's back
    ],
)
async def test_dropped_link_reconnects_once_it_advertises(
    hass: HomeAssistant,
    keep_entry: MockConfigEntry,
    keeping: SoundboksCoordinator,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
    callback_skipped: bool,
):
    await tick(hass, freezer, timedelta(minutes=1))  # the advert from setup is stale now
    if not callback_skipped:
        async_clear_advertisement_history(hass, ADDRESS)
    connects = fake_speaker.connect_count
    drop_link(fake_speaker)
    await tick(hass, freezer, RECONNECT_DELAY)
    assert fake_speaker.connect_count == connects  # not advertising yet: nothing to connect to
    await advertise(hass, keep_entry)
    await tick(hass, freezer, RECONNECT_DELAY)
    assert fake_speaker.connect_count == connects + 1
    assert keeping.client.is_connected is True
    await tick(hass, freezer, PRESENCE_GRACE + timedelta(minutes=1))
    assert keeping.present is True  # the grace the drop started has ended


@needs_bluetooth
@pytest.mark.parametrize("by_home_assistant", [False, True])
async def test_turned_off_goes_absent_and_stops_connecting(
    hass: HomeAssistant,
    keeping: SoundboksCoordinator,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
    by_home_assistant: bool,
):
    async_clear_advertisement_history(hass, ADDRESS)
    if by_home_assistant:
        await keeping.async_power_off()  # absent at once
    else:
        drop_link(fake_speaker)  # the knob: absent after the grace
    await tick(hass, freezer, PRESENCE_GRACE - timedelta(seconds=30))
    assert keeping.present is not by_home_assistant
    await tick(hass, freezer, timedelta(minutes=1))
    assert keeping.present is False
    connects = fake_speaker.connect_count
    await tick(hass, freezer, timedelta(minutes=10))
    assert fake_speaker.connect_count == connects  # absent: no attempts until it advertises
