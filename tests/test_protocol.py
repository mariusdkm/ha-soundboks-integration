"""Byte encoding and decoding, no BLE involved."""

import pytest

from custom_components.soundboks.protocol import (
    MANUFACTURER_ID,
    Advertisement,
    CommandRefusedError,
    Input,
    ProductModel,
    ProtocolError,
    SerialReply,
    SoundProfile,
    StereoRole,
    auth_accepted,
    auth_command,
    decode_eq,
    decode_firmware,
    decode_inputs,
    decode_profile,
    decode_stereo,
    decode_volume,
    encode_custom_eq,
    encode_profile,
    level_to_raw,
    parse_advertisement,
    pin_required,
    raw_to_level,
    validate_pin,
)


def test_level_round_trip_edges():
    assert level_to_raw(0.0) == 0
    assert level_to_raw(1.0) == 255
    assert level_to_raw(0.5) == 128
    assert level_to_raw(-0.2) == 0 and level_to_raw(1.7) == 255
    assert raw_to_level(255) == 1.0 and raw_to_level(0) == 0.0


def test_decode_volume():
    assert decode_volume(b"\x2d") == 0x2D
    with pytest.raises(ProtocolError):
        decode_volume(b"")


def test_profile():
    current = bytes([3, 1, 2, 3, 4, 5, 6])
    assert decode_profile(current) is SoundProfile.CUSTOM
    assert encode_profile(current, SoundProfile.BASS_PLUS) == bytes([1, 1, 2, 3, 4, 5, 6])
    # Bands never set (0) go out flat, so CUSTOM never gets invalid bands
    assert encode_profile(bytes(7), SoundProfile.CUSTOM) == bytes([3] + [51] * 6)
    with pytest.raises(ProtocolError):
        decode_profile(bytes([9, 0, 0, 0, 0, 0, 0]))
    with pytest.raises(ProtocolError):
        encode_profile(b"\x00", SoundProfile.POWER)  # not 7 bytes


def test_stereo():
    assert decode_stereo(b"\x02") is StereoRole.RIGHT
    with pytest.raises(ProtocolError):
        decode_stereo(b"\x07")


def test_inputs():
    assert decode_inputs(bytes([0, 8 | 4])) == Input.BLUETOOTH | Input.AUX
    assert decode_inputs(bytes([0, 0])) == Input(0)
    assert decode_inputs(bytes([0, 0x80 | 8])) == Input.BLUETOOTH  # unknown bits dropped
    with pytest.raises(ProtocolError):
        decode_inputs(b"\x00")


def test_security_and_pin():
    assert pin_required(b"\x01") and pin_required(b"\x03")
    assert not pin_required(b"\x00") and not pin_required(b"\x02")
    assert validate_pin(" 0042 ") == "0042"
    for bad in ("123", "12345", "12a4", ""):
        with pytest.raises(ValueError):
            validate_pin(bad)
    assert auth_command("1234") == b"aup1234"


def test_auth_accepted():
    assert auth_accepted(b"\x01\x00\x00\x00\x00") is True
    assert auth_accepted(b"\x02\x00") is False
    # The app's rule for a refusal; the firmware seen so far sends 02
    assert auth_accepted(b"\x00") is False
    assert auth_accepted(b"\x00\x01") is False
    with pytest.raises(ProtocolError):
        auth_accepted(b"\x01\x00")
    with pytest.raises(ProtocolError):
        auth_accepted(b"")


# A Go on firmware 2.4.3, as advertised (company ID stripped, as HA hands it over)
GO_ADVERTISEMENT = bytes.fromhex("00020403020102030405060708090a0b0c00")


def test_parse_advertisement_go():
    advertisement = parse_advertisement({MANUFACTURER_ID: GO_ADVERTISEMENT})
    assert advertisement == Advertisement(
        model=ProductModel.SB75, firmware=(2, 4, 3), pin_enabled=False, locked=False
    )
    assert advertisement.firmware_version == "2.4.3"
    assert advertisement.model_name == "SOUNDBOKS Go"
    assert advertisement.speaks_serial


def test_parse_advertisement_security_bits_and_old_sb3():
    advertisement = parse_advertisement({MANUFACTURER_ID: bytes([0x03, 2, 1, 0, 0])})
    assert advertisement is not None
    assert advertisement.pin_enabled and advertisement.locked
    assert advertisement.model == ProductModel.SB3  # old units send 0


def test_parse_advertisement_rpc_firmware_and_unknown_model():
    advertisement = parse_advertisement({MANUFACTURER_ID: bytes([0, 3, 0, 0, 99])})
    assert advertisement is not None
    assert not advertisement.speaks_serial
    assert advertisement.model_name == "SOUNDBOKS (model 99)"


def test_parse_advertisement_missing_or_short():
    assert parse_advertisement({}) is None
    assert parse_advertisement({0x004C: GO_ADVERTISEMENT}) is None
    assert parse_advertisement({MANUFACTURER_ID: b"\x00\x02\x04"}) is None


def test_serial_reply_framed_over_several_packets():
    reply = SerialReply("gss")
    assert reply.feed(b"\x01\x00\x00\x00\x07" + b"123") is None
    assert reply.feed(b"4abc") == b"1234abc"


def test_serial_reply_empty_frame():
    assert SerialReply("gst").feed(b"\x01\x00\x00\x00\x00") == b""


def test_serial_reply_unframed():
    assert SerialReply("pmv").feed(b"sb75") == b"sb75"
    # Starts like a frame header, but this opcode never sends one
    assert SerialReply("fwv").feed(b"\x01\x2e\x02") == b"\x01\x2e\x02"


def test_serial_reply_error_code():
    with pytest.raises(CommandRefusedError, match="29"):
        SerialReply("tpg").feed(b"\x02\x29")
    with pytest.raises(CommandRefusedError):
        SerialReply("gdc").feed(b"\x02\x06")


def test_serial_reply_unframed_payload_starting_like_an_error():
    # A Go's firmware 2.4.3; only a 2-byte answer is an error
    assert SerialReply("fwv").feed(b"\x02\x2e\x04\x2e\x03") == b"\x02\x2e\x04\x2e\x03"


def test_serial_reply_malformed():
    with pytest.raises(ProtocolError):
        SerialReply("gdc").feed(b"\x01\x00")  # header cut short
    with pytest.raises(ProtocolError):
        SerialReply("gdc").feed(b"\x05hello")  # no header at all
    reply = SerialReply("gdc")
    reply.feed(b"\x01\x00\x00\x00\x02a")
    with pytest.raises(ProtocolError):
        reply.feed(b"bc")  # longer than announced


def test_eq_bands():
    # 0 is never set (flat); otherwise (dB + 10) * 5 + 1
    assert decode_eq(bytes([3, 0, 1, 26, 51, 76, 101])) == (0, -10, -5, 0, 5, 10)
    assert encode_custom_eq((0, -10, -5, 0, 5, 10)) == bytes([3, 51, 1, 26, 51, 76, 101])
    # Off-grid bytes, as some other client might write, read as the nearest dB
    assert decode_eq(bytes([0, 3, 4, 49, 54, 99, 200])) == (-10, -9, 0, 1, 10, 10)


def test_encode_custom_eq_rejects_bad_bands():
    with pytest.raises(ValueError):
        encode_custom_eq((0,) * 5)
    with pytest.raises(ValueError):
        encode_custom_eq((0, 0, 0, 0, 0, 11))


def test_decode_firmware():
    assert decode_firmware(b"2.4.2") == "2.4.2"
    # A Go on 2.4.3 sends the numbers as bytes between ASCII dots
    assert decode_firmware(bytes([2, 0x2E, 4, 0x2E, 3])) == "2.4.3"
    assert decode_firmware(bytes([0, 0x2E, 12, 0x2E, 1, 0, 0])) == "0.12.1"
    # A zero patch is a 00 byte, not padding
    assert decode_firmware(bytes([2, 0x2E, 4, 0x2E, 0])) == "2.4.0"
    assert decode_firmware(b"2.4.0\x00\x00") == "2.4.0"
