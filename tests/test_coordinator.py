"""Setup, polling and presence, against the fake speaker."""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.bluetooth import (
    MONOTONIC_TIME,
    BluetoothChange,
    async_clear_advertisement_history,
    async_get_advertisement_callback,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.soundboks.const import CONF_PIN, DOMAIN, PRESENCE_GRACE
from custom_components.soundboks.protocol import FIRMWARE_UUID, POWER_UUID, VOLUME_UUID

from .conftest import (
    ADDRESS,
    FakeSpeaker,
    advertise,
    go_unavailable,
    make_ble_device,
    make_service_info,
    setup_entry,
)

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


async def test_setup_with_speaker_off_succeeds(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    assert mock_entry.state is ConfigEntryState.LOADED
    assert coordinator.data is None
    assert coordinator.present is False
    assert fake_speaker.connect_count == 0


async def test_advertisement_reads_state(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    assert coordinator.present is True
    assert coordinator.data.volume == 0x2D
    assert coordinator.firmware == "2.4.2"
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ADDRESS), mock_entry.entry_id
    )
    assert device is not None and device.sw_version == "2.4.2"
    assert coordinator.client.is_connected is False


async def test_poll_skipped_when_absent(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await mock_entry.runtime_data.async_refresh()
    assert fake_speaker.connect_count == 0


async def test_busy_speaker_keeps_last_state(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    before = coordinator.data
    fake_speaker.unreachable = True
    await coordinator.async_refresh()
    assert coordinator.data == before
    assert coordinator.last_update_success is True


async def test_wrong_pin_fails_auth(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    fake_speaker.set_pin("9999")
    entry = MockConfigEntry(
        domain=mock_entry.domain,
        unique_id=mock_entry.unique_id,
        title=mock_entry.title,
        data={**mock_entry.data, CONF_PIN: "1234"},
    )
    await setup_entry(hass, entry)
    await advertise(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)


async def test_power_off_marks_absent_immediately(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    await coordinator.async_power_off()
    assert (POWER_UUID, b"\x00") in fake_speaker.writes
    assert coordinator.present is False


async def test_set_volume_updates_state_without_poll(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    await coordinator.async_set_volume(0x80)
    assert (VOLUME_UUID, b"\x80") in fake_speaker.writes
    assert coordinator.data.volume == 0x80
    await coordinator.client.disconnect()


async def test_command_when_absent_raises_without_connecting(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    with pytest.raises(HomeAssistantError):
        await mock_entry.runtime_data.async_set_volume(10)
    assert fake_speaker.connect_count == 0


async def test_command_when_unreachable_raises_ha_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    fake_speaker.unreachable = True
    with pytest.raises(HomeAssistantError) as raised:
        await mock_entry.runtime_data.async_set_volume(10)
    assert raised.value.translation_key == "unreachable"


async def test_knob_notification_updates_state(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    await coordinator.async_set_volume(0x10)
    fake_speaker.notify(VOLUME_UUID, b"\x50")
    assert coordinator.data.volume == 0x50
    await coordinator.client.disconnect()


async def test_unavailable_respects_grace(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
):
    assert PRESENCE_GRACE == timedelta(minutes=5)
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    go_unavailable(hass, mock_entry)
    freezer.tick(timedelta(minutes=4))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.present is True
    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.present is False


async def test_advertisement_cancels_grace(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    go_unavailable(hass, mock_entry)
    await advertise(hass, mock_entry)
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.present is True


async def test_unload_disconnects(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    await coordinator.async_set_volume(10)
    assert coordinator.client.is_connected
    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    assert coordinator.client.is_connected is False
    assert mock_entry.state is ConfigEntryState.NOT_LOADED


def later(seconds: float):
    """Make the coordinator's clock run seconds ahead of the advertisements."""
    now = MONOTONIC_TIME()
    return patch(
        "custom_components.soundboks.coordinator.MONOTONIC_TIME", return_value=now + seconds
    )


async def test_firmware_failure_keeps_state(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    fake_speaker.failing_reads.add(FIRMWARE_UUID)
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    assert coordinator.data is not None and coordinator.data.volume == 0x2D
    assert coordinator.firmware is None


async def test_poll_skipped_when_not_recently_seen(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    before, connects = coordinator.data, fake_speaker.connect_count
    with later(60):
        await coordinator.async_refresh()
    assert fake_speaker.connect_count == connects
    assert coordinator.data == before
    assert coordinator.present is True  # still within the grace window


async def test_command_fails_fast_when_not_recently_seen(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    connects = fake_speaker.connect_count
    with later(60), pytest.raises(HomeAssistantError) as raised:
        await mock_entry.runtime_data.async_set_volume(10)
    assert raised.value.translation_key == "unreachable"
    assert fake_speaker.connect_count == connects


async def test_command_uses_open_link_without_adverts(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    """While HA holds the link the speaker doesn't advertise: commands must still go through."""
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    await coordinator.async_set_volume(10)
    with later(20):
        await coordinator.async_set_volume(20)
    assert (VOLUME_UUID, b"\x14") in fake_speaker.writes
    await coordinator.client.disconnect()


async def test_stale_replay_does_not_mark_present(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    coordinator.async_handle_advertisement(
        make_service_info(age=3600), BluetoothChange.ADVERTISEMENT
    )
    await hass.async_block_till_done()
    assert coordinator.present is False
    assert fake_speaker.connect_count == 0


async def test_reboot_incomplete_raises_specific_error(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    fake_speaker.failing_connects_after_power_write = 100
    with pytest.raises(HomeAssistantError) as raised:
        await mock_entry.runtime_data.async_reboot()
    assert raised.value.translation_key == "reboot_incomplete"


async def test_identical_adverts_keep_speaker_reachable(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    """HA only calls integration callbacks when an advert changes; the speaker repeats itself.

    Goes through HA's real Bluetooth manager rather than calling the coordinator directly.
    """
    await setup_entry(hass, mock_entry)
    inject = async_get_advertisement_callback(hass)
    inject(make_service_info())
    await hass.async_block_till_done()
    assert mock_entry.runtime_data.present is True
    # 20 s later: the same advert keeps arriving, but no callback reaches the integration
    repeat = make_service_info()
    repeat.time += 20
    inject(repeat)
    await hass.async_block_till_done()
    with later(20):
        await mock_entry.runtime_data.async_set_volume(0x20)
    assert (VOLUME_UUID, b"\x20") in fake_speaker.writes
    await mock_entry.runtime_data.client.disconnect()


async def turn_back_on(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    """Some seconds pass, then the speaker advertises exactly as it did before it went off."""
    freezer.tick(timedelta(seconds=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await advertise(hass, entry)


async def test_back_on_after_ha_power_off(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    await coordinator.async_power_off()
    await turn_back_on(hass, mock_entry, freezer)
    assert coordinator.present is True
    await coordinator.async_set_volume(0x30)
    assert (VOLUME_UUID, b"\x30") in fake_speaker.writes
    await coordinator.client.disconnect()


async def test_back_on_after_reboot(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    await coordinator.async_reboot()
    await turn_back_on(hass, mock_entry, freezer)
    assert coordinator.present is True


async def test_advert_right_after_power_off_ignored(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    """A last advertisement relayed as the speaker shuts down doesn't bring it back."""
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    await coordinator.async_power_off()
    async_clear_advertisement_history(hass, ADDRESS)  # so the advert below is delivered
    await advertise(hass, mock_entry)
    assert coordinator.present is False


@pytest.fixture
def orphaned_link() -> Iterator[AsyncMock]:
    """BlueZ holds a link to the speaker that HA never owned; the mock closes it."""
    with (
        patch(
            "custom_components.soundboks.client.get_connected_devices",
            AsyncMock(return_value=[make_ble_device()]),
        ),
        patch("custom_components.soundboks.client.close_stale_connections", AsyncMock()) as close,
    ):
        yield close


async def test_command_closes_orphaned_link(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    orphaned_link: AsyncMock,
):
    """The orphan stops the adverts, so without closing it the speaker stays unreachable."""
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    with later(60):
        await mock_entry.runtime_data.async_set_volume(10)
    orphaned_link.assert_awaited()
    assert (VOLUME_UUID, b"\x0a") in fake_speaker.writes
    await mock_entry.runtime_data.client.disconnect()


async def test_poll_closes_orphaned_link(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    orphaned_link: AsyncMock,
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    connects = fake_speaker.connect_count
    fake_speaker.values[VOLUME_UUID] = bytes([99])
    with later(60):
        await coordinator.async_refresh()
    orphaned_link.assert_awaited()
    assert fake_speaker.connect_count == connects + 1
    assert coordinator.data.volume == 99


async def test_volume_burst_writes_only_first_and_latest(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    """A dragged slider: calls waiting behind a write collapse into the latest one."""
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    gate = asyncio.Event()
    written: list[int] = []

    async def slow_set_volume(raw: int) -> None:
        written.append(raw)
        await gate.wait()

    with patch.object(coordinator.client, "set_volume", slow_set_volume):
        calls = [
            asyncio.create_task(coordinator.async_set_volume(raw)) for raw in range(10, 60, 10)
        ]
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(*calls)
    assert written == [10, 50]
    assert coordinator.data.volume == 50


async def test_unreachable_command_holds_back_the_next(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    fake_speaker.unreachable = True
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_volume(10)
    fake_speaker.unreachable = False
    connects = fake_speaker.connect_count
    with pytest.raises(HomeAssistantError) as raised:
        await coordinator.async_set_volume(20)
    assert raised.value.translation_key == "unreachable"
    assert fake_speaker.connect_count == connects  # failed without trying


async def test_successful_poll_ends_the_hold_back(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    await advertise(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    fake_speaker.unreachable = True
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_volume(10)
    fake_speaker.unreachable = False
    await coordinator.async_refresh()
    await coordinator.async_set_volume(20)
    assert (VOLUME_UUID, b"\x14") in fake_speaker.writes
    await coordinator.client.disconnect()
