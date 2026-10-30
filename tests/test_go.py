"""What a SOUNDBOKS Go (firmware 2.4.3) does differently, and the BlueZ workarounds for it."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice

from custom_components.soundboks import bluez
from custom_components.soundboks.client import SoundboksClient
from custom_components.soundboks.protocol import (
    FIRMWARE_UUID,
    PROFILE_UUID,
    SERIAL_RX_SHORT_UUID,
    SERIAL_RX_UUID,
    SERIAL_TX_SHORT_UUID,
    SERIAL_TX_UUID,
    VOLUME_UUID,
    InvalidPinError,
    SoundProfile,
    SpeakerUnreachableError,
    UnsupportedSpeakerError,
)

from .conftest import ADDRESS, NAME, FakeSpeaker, make_ble_device

BLUEZ_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


def bluez_device() -> BLEDevice:
    return BLEDevice(ADDRESS, NAME, {"path": BLUEZ_PATH, "props": {}})


@pytest.fixture(autouse=True)
def no_system_bus() -> Iterator[None]:
    """These devices have a BlueZ path; tests reach the bus only through a patched _call."""
    with patch.object(bluez, "MessageBus", side_effect=AssertionError("no D-Bus in tests")):
        yield


@pytest.fixture
def forget() -> Iterator[AsyncMock]:
    """BlueZ's forget, with bleak-retry-connector's cache clearing alongside it."""
    with (
        patch(
            "custom_components.soundboks.client.forget_device", AsyncMock(return_value=True)
        ) as forget,
        patch("custom_components.soundboks.client.clear_cache", AsyncMock()) as clear,
    ):
        forget.clear_cache = clear
        yield forget


async def test_serial_channel_under_16_bit_uuids(fake_speaker: FakeSpeaker):
    fake_speaker.serial = (SERIAL_TX_SHORT_UUID, SERIAL_RX_SHORT_UUID)
    fake_speaker.set_pin("1234")
    client = SoundboksClient(make_ble_device(), "1234")
    await client.set_volume(50)
    assert fake_speaker.writes[0] == (SERIAL_TX_SHORT_UUID, b"aup1234")
    with pytest.raises(InvalidPinError):
        await SoundboksClient(make_ble_device(), "9999").verify_pin("9999")
    await client.disconnect()


async def test_no_serial_channel_is_fine_without_pin(fake_speaker: FakeSpeaker):
    fake_speaker.hidden = {SERIAL_TX_UUID, SERIAL_RX_UUID}
    client = SoundboksClient(make_ble_device(), None)
    await client.set_volume(50)
    assert fake_speaker.values[VOLUME_UUID] == b"\x32"
    await client.disconnect()


async def test_no_serial_channel_with_pin_is_unsupported(fake_speaker: FakeSpeaker):
    fake_speaker.hidden = {SERIAL_TX_UUID, SERIAL_RX_UUID}
    fake_speaker.set_pin("1234")
    with pytest.raises(UnsupportedSpeakerError):
        await SoundboksClient(make_ble_device(), "1234").set_volume(50)


async def test_raw_firmware_version(fake_speaker: FakeSpeaker):
    fake_speaker.values[FIRMWARE_UUID] = bytes([2, 0x2E, 4, 0x2E, 3])
    assert await SoundboksClient(make_ble_device(), None).read_firmware() == "2.4.3"


async def test_eq_read_and_written(fake_speaker: FakeSpeaker):
    fake_speaker.values[PROFILE_UUID] = bytes([2, 0, 0, 0, 0, 0, 0])  # never set
    client = SoundboksClient(make_ble_device(), None)
    state = await client.read_state()
    assert state.profile is SoundProfile.INDOOR and state.eq == (0,) * 6
    await client.set_custom_eq((4, 2, 0, 0, -2, -4))
    assert fake_speaker.values[PROFILE_UUID] == bytes([3, 71, 61, 51, 51, 41, 31])
    await client.disconnect()


async def test_stale_table_is_cleared_once(fake_speaker: FakeSpeaker, forget: AsyncMock):
    """A table cached from a BR/EDR connection lacks the controls: forget it, then retry."""
    fake_speaker.hidden = {VOLUME_UUID}
    client = SoundboksClient(bluez_device(), None)
    with pytest.raises(SpeakerUnreachableError):
        await client.read_state()
    forget.assert_awaited_once()
    forget.clear_cache.assert_awaited_once_with(ADDRESS)
    assert client.is_connected is False
    # Still missing after a fresh table: really unsupported, and not forgotten again
    with pytest.raises(UnsupportedSpeakerError):
        await client.check_supported()
    forget.assert_awaited_once()


async def test_stale_table_left_alone_off_bluez(fake_speaker: FakeSpeaker, forget: AsyncMock):
    """Through a proxy there is no BlueZ table to clear: just report the speaker unsupported."""
    fake_speaker.hidden = {VOLUME_UUID}
    with pytest.raises(UnsupportedSpeakerError):
        await SoundboksClient(make_ble_device(), None).check_supported()
    forget.assert_not_awaited()


async def test_le_preferred_before_each_connection(fake_speaker: FakeSpeaker):
    with patch("custom_components.soundboks.client.prefer_le", AsyncMock()) as prefer:
        client = SoundboksClient(bluez_device(), None)
        await client.read_state()
        await client.read_state()
    assert prefer.await_count == 2 == fake_speaker.connect_count


def test_bluez_path():
    assert bluez.bluez_path(bluez_device()) == BLUEZ_PATH
    assert bluez.bluez_path(make_ble_device()) is None
    assert bluez.bluez_path(BLEDevice(ADDRESS, NAME, {"source": "esphome-proxy"})) is None


async def test_prefer_le_sets_preferred_bearer():
    with patch.object(bluez, "_call", AsyncMock(return_value=None)) as call:
        await bluez.prefer_le(bluez_device())
        await bluez.prefer_le(make_ble_device())  # no BlueZ path: nothing to do
    call.assert_awaited_once()
    path, interface, member, signature, body = call.await_args.args
    assert (path, interface, member, signature) == (
        BLUEZ_PATH,
        "org.freedesktop.DBus.Properties",
        "Set",
        "ssv",
    )
    assert body[:2] == ["org.bluez.Device1", "PreferredBearer"] and body[2].value == "le"


def device_properties(**values: bool):
    return patch.object(bluez, "_device_properties", AsyncMock(return_value=values))


async def test_forget_device_removes_from_adapter():
    with (
        device_properties(Paired=False, Bonded=False),
        patch.object(bluez, "_call", AsyncMock(return_value=None)) as call,
    ):
        assert await bluez.forget_device(bluez_device()) is True
    call.assert_awaited_once_with(
        "/org/bluez/hci0", "org.bluez.Adapter1", "RemoveDevice", "o", [BLUEZ_PATH]
    )
    with (
        device_properties(Paired=False),
        patch.object(bluez, "_call", AsyncMock(return_value="org.bluez.Error.DoesNotExist")),
    ):
        assert await bluez.forget_device(bluez_device()) is False


@pytest.mark.parametrize("values", [{"Paired": True}, {"Bonded": True}])
async def test_forget_device_keeps_a_paired_device(values: dict[str, bool]):
    """Removing it would delete the pairing, e.g. for audio from this host."""
    with device_properties(**values), patch.object(bluez, "_call", AsyncMock()) as call:
        assert await bluez.forget_device(bluez_device()) is False
    call.assert_not_awaited()


async def test_forget_device_keeps_it_when_properties_are_unknown():
    with (
        patch.object(bluez, "_request", AsyncMock(return_value="no system bus")),
        patch.object(bluez, "_call", AsyncMock()) as call,
    ):
        assert await bluez.forget_device(bluez_device()) is False
    call.assert_not_awaited()


async def test_stale_table_is_cleared_again_after_a_full_table(
    fake_speaker: FakeSpeaker, forget: AsyncMock
):
    """BlueZ may recreate the device and cache a BR/EDR table again later."""
    client = SoundboksClient(bluez_device(), None)
    fake_speaker.hidden = {VOLUME_UUID}
    with pytest.raises(SpeakerUnreachableError):
        await client.read_state()
    fake_speaker.hidden = set()
    await client.read_state()
    fake_speaker.hidden = {VOLUME_UUID}
    with pytest.raises(SpeakerUnreachableError):
        await client.read_state()
    assert forget.await_count == 2


async def test_bus_failure_is_reported_not_raised():
    with patch.object(bluez, "MessageBus", side_effect=OSError("no bus")):
        assert "no system bus" in await bluez._call("/", "i", "m", "", [])
        await bluez.prefer_le(bluez_device())  # logged, not raised


async def test_no_orphaned_link_left_alone(fake_speaker: FakeSpeaker):
    client = SoundboksClient(bluez_device(), None)
    with (
        patch(
            "custom_components.soundboks.client.get_connected_devices", AsyncMock(return_value=[])
        ),
        patch("custom_components.soundboks.client.close_stale_connections", AsyncMock()) as close,
    ):
        assert await client.close_orphaned_link() is False
        await client.set_volume(10)  # our own link is never an orphan
        assert await client.close_orphaned_link() is False
    close.assert_awaited_once()  # only the clean-up before connecting
    await client.disconnect()


async def test_stale_links_closed_before_each_connection(fake_speaker: FakeSpeaker):
    client = SoundboksClient(bluez_device(), None)
    with patch("custom_components.soundboks.client.close_stale_connections", AsyncMock()) as close:
        await client.read_state()
        await client.read_state()
    assert close.await_count == fake_speaker.connect_count == 2
