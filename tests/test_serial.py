"""The client's serial queries, auto-boot arming and sound status notifications."""

import pytest

from custom_components.soundboks.client import SoundboksClient
from custom_components.soundboks.protocol import (
    AUTO_BOOT_ONCE,
    INPUTS_UUID,
    POWER_UUID,
    Input,
    SpeakerUnreachableError,
)

from .conftest import FakeSpeaker, make_ble_device


def make_client(**kwargs) -> SoundboksClient:
    return SoundboksClient(make_ble_device(), None, **kwargs)


async def test_arm_auto_boot_does_not_wait_for_the_missing_answer(fake_speaker: FakeSpeaker):
    fake_speaker.hang_on_auto_boot = True
    client = make_client()
    await client.arm_auto_boot()  # AUTO_BOOT_WAIT is patched short; it would hang otherwise
    assert fake_speaker.writes[-1] == (POWER_UUID, AUTO_BOOT_ONCE)
    # The unanswered write blocks the link: it is closed rather than reused
    assert client.is_connected is False


async def test_arm_auto_boot_tolerates_a_dropped_link(fake_speaker: FakeSpeaker):
    client = make_client()  # this fake drops the link on every power write
    await client.arm_auto_boot()
    assert (POWER_UUID, AUTO_BOOT_ONCE) in fake_speaker.writes
    assert client.is_connected is False


async def test_query_framed_unframed_and_refused(fake_speaker: FakeSpeaker):
    fake_speaker.serial_replies = {
        b"fwv": [b"\x02\x2e\x04\x2e\x03"],
        b"gss": [b"\x01\x00\x00\x00\x07123", b"4abc"],
        b"tpg": [b"\x02\x29"],
        b"gst": [b"\x01\x00\x00\x00\x01\x00"],
    }
    client = make_client()
    replies = await client.query(("fwv", "gss", "tpg", "gst"))
    assert replies == {
        "fwv": b"\x02\x2e\x04\x2e\x03",
        "gss": b"1234abc",
        "tpg": "refused: error 29",
        "gst": b"\x00",
    }
    assert client.is_connected is False


async def test_query_stops_at_an_unanswered_command(fake_speaker: FakeSpeaker, monkeypatch):
    monkeypatch.setattr("custom_components.soundboks.client.RESPONSE_TIMEOUT", 0.01)
    fake_speaker.serial_replies = {b"pmv": [b"sb75"], b"gcm": [b"\x01\x00\x00\x00\x09partial"]}
    client = make_client()
    replies = await client.query(("pmv", "gcm", "gss"))
    assert replies["pmv"] == b"sb75"
    assert replies["gcm"].startswith("no valid answer")
    assert "gss" not in replies  # never sent: a late gcm packet would be taken for its reply
    assert not any(uuid != POWER_UUID and data == b"gss" for uuid, data in fake_speaker.writes)


async def test_query_reports_malformed_reply(fake_speaker: FakeSpeaker):
    fake_speaker.serial_replies = {b"gdc": [b"\x07junk"]}
    replies = await make_client().query(("gdc", "gss"))
    assert replies["gdc"].startswith("no valid answer")
    assert "gss" not in replies


async def test_inputs_notifications_reach_the_callback(fake_speaker: FakeSpeaker):
    seen: list[Input] = []
    client = make_client(on_inputs=seen.append)
    await client.set_volume(10)  # a command keeps the link open
    fake_speaker.notify(INPUTS_UUID, bytes.fromhex("000400020008"))
    fake_speaker.notify(INPUTS_UUID, b"\x00")  # malformed: ignored
    fake_speaker.notify(INPUTS_UUID, bytes.fromhex("000c01020008"))
    assert seen == [Input.AUX, Input.AUX | Input.BLUETOOTH]


async def test_speaker_without_sound_status_still_connects(fake_speaker: FakeSpeaker):
    fake_speaker.hidden = {INPUTS_UUID}
    client = make_client(on_inputs=lambda _inputs: pytest.fail("no sound status to notify"))
    await client.set_volume(10)
    assert INPUTS_UUID not in fake_speaker.client.callbacks


async def test_refused_auto_boot_write_is_an_error(fake_speaker: FakeSpeaker):
    """A write refused while the link stays up armed nothing: callers must not cut the power."""
    fake_speaker.failing_writes = {POWER_UUID}
    with pytest.raises(SpeakerUnreachableError):
        await make_client().arm_auto_boot()
