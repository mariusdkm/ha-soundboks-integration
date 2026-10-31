"""The BLE client against a fake speaker: connection policy, PIN, writes."""

import asyncio

import pytest

from custom_components.soundboks.client import SoundboksClient
from custom_components.soundboks.protocol import (
    AUTO_BOOT_ONCE,
    FIRMWARE_UUID,
    POWER_OFF,
    POWER_UUID,
    PROFILE_UUID,
    SECURITY_UUID,
    SERIAL_TX_UUID,
    STEREO_UUID,
    VOLUME_UUID,
    Input,
    InvalidPinError,
    PinRequiredError,
    RebootIncompleteError,
    SoundProfile,
    SpeakerState,
    SpeakerUnreachableError,
    StereoRole,
    UnsupportedSpeakerError,
)

from .conftest import FakeSpeaker, make_ble_device, power_writes


def make_client(pin: str | None = None, **kwargs) -> SoundboksClient:
    return SoundboksClient(make_ble_device(), pin, **kwargs)


async def test_read_state_disconnects_when_it_opened_the_link(fake_speaker: FakeSpeaker):
    client = make_client()
    state = await client.read_state()
    assert state == SpeakerState(
        0x2D, SoundProfile.POWER, StereoRole.MONO, Input.BLUETOOTH, log_amount=0x60
    )
    assert fake_speaker.connect_count == 1  # gla rode on the poll's link
    assert client.is_connected is False


async def test_command_keeps_link_for_idle_window(fake_speaker: FakeSpeaker):
    client = make_client(idle_timeout=0.05)
    await client.set_volume(100)
    assert client.is_connected
    assert (VOLUME_UUID, b"\x64") in fake_speaker.writes
    await asyncio.sleep(0.1)
    assert client.is_connected is False


async def test_poll_inside_idle_window_does_not_extend_it(fake_speaker: FakeSpeaker):
    client = make_client(idle_timeout=0.1)
    await client.set_volume(100)
    await asyncio.sleep(0.06)
    await client.read_state()
    await asyncio.sleep(0.06)
    assert client.is_connected is False


async def test_burst_reuses_connection(fake_speaker: FakeSpeaker):
    client = make_client()
    for raw in (10, 20, 30):
        await client.set_volume(raw)
    assert fake_speaker.connect_count == 1


async def test_concurrent_operations_share_one_connection(fake_speaker: FakeSpeaker):
    client = make_client()
    await asyncio.gather(client.set_volume(1), client.set_volume(2), client.read_state())
    assert fake_speaker.connect_count == 1
    assert [w for w in fake_speaker.writes if w[0] == VOLUME_UUID] == [
        (VOLUME_UUID, b"\x01"),
        (VOLUME_UUID, b"\x02"),
    ]


async def test_power_off_treats_dropped_link_as_success(fake_speaker: FakeSpeaker):
    client = make_client()
    await client.power_off()
    assert (POWER_UUID, b"\x00") in fake_speaker.writes
    assert client.is_connected is False


async def test_power_off_without_link_drop(fake_speaker: FakeSpeaker):
    fake_speaker.drop_on_power_write = False
    client = make_client()
    await client.power_off()
    assert client.is_connected is False


@pytest.mark.parametrize(
    ("hang_on_auto_boot", "failing_connects"),
    [(False, 0), (True, 0), (False, 2)],
    ids=["link_dropped", "arm_unanswered", "reconnect_retried"],
)
async def test_reboot_writes_auto_boot_then_off(
    fake_speaker: FakeSpeaker, hang_on_auto_boot: bool, failing_connects: int
):
    fake_speaker.hang_on_auto_boot = hang_on_auto_boot
    fake_speaker.failing_connects_after_power_write = failing_connects
    client = make_client()
    await client.reboot()
    assert power_writes(fake_speaker) == [AUTO_BOOT_ONCE, POWER_OFF]
    assert fake_speaker.connect_count == 2
    assert client.is_connected is False


async def test_pin_auth_sent_when_required(fake_speaker: FakeSpeaker):
    fake_speaker.set_pin("1234")
    client = make_client("1234")
    await client.set_volume(50)
    assert fake_speaker.writes[0] == (SERIAL_TX_UUID, b"aup1234")
    assert fake_speaker.values[VOLUME_UUID] == b"\x32"


async def test_no_auth_without_pin(fake_speaker: FakeSpeaker):
    client = make_client("1234")
    await client.set_volume(50)
    assert all(uuid != SERIAL_TX_UUID for uuid, _ in fake_speaker.writes)


@pytest.mark.parametrize(("pin", "error"), [("1234", InvalidPinError), (None, PinRequiredError)])
async def test_wrong_or_missing_pin_raises(
    fake_speaker: FakeSpeaker, pin: str | None, error: type[Exception]
):
    fake_speaker.set_pin("9999")
    client = make_client(pin)
    with pytest.raises(error):
        await client.set_volume(50)
    assert client.is_connected is False
    assert fake_speaker.values[VOLUME_UUID] == b"\x2d"


async def test_pin_required_and_verify_pin(fake_speaker: FakeSpeaker):
    fake_speaker.set_pin("1234")
    client = make_client(None)
    assert await client.pin_required() is True
    with pytest.raises(InvalidPinError):
        await client.verify_pin("0000")
    await client.verify_pin("1234")
    await client.set_volume(50)  # the verified PIN is used from now on
    assert fake_speaker.values[VOLUME_UUID] == b"\x32"


async def test_unreachable_raises(fake_speaker: FakeSpeaker):
    fake_speaker.unreachable = True
    with pytest.raises(SpeakerUnreachableError):
        await make_client().set_volume(10)
    with pytest.raises(SpeakerUnreachableError):
        await SoundboksClient(None, None).read_state()


async def test_write_failure_mid_operation_is_unreachable(fake_speaker: FakeSpeaker):
    client = make_client()
    await client.set_volume(10)
    fake_speaker.client.is_connected = False  # link lost without a callback
    fake_speaker.client = None
    fake_speaker.unreachable = True
    with pytest.raises(SpeakerUnreachableError):
        await client.set_volume(20)


async def test_set_profile_keeps_bands(fake_speaker: FakeSpeaker):
    fake_speaker.values[PROFILE_UUID] = bytes([3, 1, 2, 3, 4, 5, 6])
    client = make_client()
    await client.set_profile(SoundProfile.INDOOR)
    assert (PROFILE_UUID, bytes([2, 1, 2, 3, 4, 5, 6])) in fake_speaker.writes


async def test_set_stereo(fake_speaker: FakeSpeaker):
    client = make_client()
    await client.set_stereo(StereoRole.LEFT)
    assert (STEREO_UUID, b"\x01") in fake_speaker.writes


async def test_set_volume_rejects_out_of_range(fake_speaker: FakeSpeaker):
    with pytest.raises(ValueError):
        await make_client().set_volume(256)


async def test_check_supported(fake_speaker: FakeSpeaker):
    client = make_client()
    await client.check_supported()
    del fake_speaker.values[PROFILE_UUID]
    with pytest.raises(UnsupportedSpeakerError):
        await client.check_supported()
    assert client.is_connected is False


async def test_check_supported_ignores_pin(fake_speaker: FakeSpeaker):
    fake_speaker.set_pin("1234")
    await make_client(None).check_supported()
    assert fake_speaker.values[SECURITY_UUID] == b"\x01"


@pytest.mark.parametrize(
    ("raw", "version"),
    [(b"2.4.2", "2.4.2"), (bytes([2, 0x2E, 4, 0x2E, 3]), "2.4.3")],  # a Go sends raw numbers
)
async def test_read_firmware(fake_speaker: FakeSpeaker, raw: bytes, version: str):
    fake_speaker.values[FIRMWARE_UUID] = raw
    assert await make_client().read_firmware() == version


async def test_volume_notifications_forwarded(fake_speaker: FakeSpeaker):
    seen: list[int] = []
    client = make_client(on_volume=seen.append)
    await client.set_volume(10)
    fake_speaker.notify(VOLUME_UUID, b"\x40")
    assert seen == [0x40]


async def test_reboot_reports_armed_restart(fake_speaker: FakeSpeaker):
    fake_speaker.failing_connects_after_power_write = 100
    client = make_client()
    with pytest.raises(RebootIncompleteError):
        await client.reboot()
    assert power_writes(fake_speaker) == [AUTO_BOOT_ONCE]
