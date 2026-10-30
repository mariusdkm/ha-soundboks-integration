"""The speaker's BLE protocol: characteristic UUIDs and their byte layouts.

Pure encoding and decoding, no BLE and no Home Assistant, so every rule here is unit-tested on its
own. docs/PROTOCOL.md describes the protocol in prose.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum, IntFlag

ADVERTISED_SERVICE_UUID = "f5c26570-64ec-4906-b998-6a7302879a2b"
VOLUME_UUID = "7649b19f-c605-46e2-98f8-6c1808e0cfb4"
POWER_UUID = "11ad501d-fa86-43cc-8d92-5a27ee672f1a"
PROFILE_UUID = "57a394fb-6d89-4105-8f07-bf730338a9b2"
STEREO_UUID = "7d0d651e-62ae-4ef2-a727-0e8f3e9b4dfb"
INPUTS_UUID = "8d36814f-3741-4cb5-a018-ef4a2d8e24f1"
SECURITY_UUID = "93aa965c-98b8-4433-b6bb-41ce61a8db9a"
# Serial command channel: commands are written to TX, responses arrive as RX notifications
SERIAL_TX_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"
SERIAL_RX_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"
# The same channel as some firmware declares it (seen on a Go, firmware 2.4.3): 16-bit UUIDs made
# of the last four digits of the ones above
SERIAL_TX_SHORT_UUID = "00009bb3-0000-1000-8000-00805f9b34fb"
SERIAL_RX_SHORT_UUID = "00009616-0000-1000-8000-00805f9b34fb"
SERIAL_TX_UUIDS = (SERIAL_TX_UUID, SERIAL_TX_SHORT_UUID)
SERIAL_RX_UUIDS = (SERIAL_RX_UUID, SERIAL_RX_SHORT_UUID)
FIRMWARE_UUID = "8980ccc7-e6d4-4f9d-9540-3c62fa0a5a74"
# A speaker without these is another generation, which this integration doesn't speak
REQUIRED_UUIDS = (VOLUME_UUID, POWER_UUID, PROFILE_UUID)

POWER_OFF = b"\x00"
# Power stays on; the speaker boots by itself the next time it gets power, once. A power-off
# right after it therefore restarts the speaker, and so does cutting its power and restoring it.
# The speaker never answers this write (BlueZ gives up after ~30 s with ATT 0x0e), yet it works.
# Never write 01 02 ("always"): the knob can then no longer turn the speaker off.
AUTO_BOOT_ONCE = b"\x01\x01"

MAX_RAW_VOLUME = 255
PROFILE_LENGTH = 7  # mode byte, then six EQ band bytes
EQ_BANDS = 6
EQ_MIN_DB = -10
EQ_MAX_DB = 10
FLAT_EQ = (0,) * EQ_BANDS
# A band byte is (dB + 10) * 5 + 1, so 1-101 in steps of 5 per dB; 0 means never set, i.e. flat
EQ_UNSET = 0
PIN_ENABLED = 0x01  # security level bit
PIN_LENGTH = 4
AUTH_ACCEPTED = b"\x01\x00\x00\x00\x00"
# First bytes of a refusal: 02 is what the firmware sends for errors, 00 what the app checks for
ERROR_RESPONSES = (0x00, 0x02)

# Advertisement manufacturer data: security bits, firmware major.minor.patch, product model,
# then the device ID and SKAA ID, which the integration doesn't use
MANUFACTURER_ID = 0x0858
ADVERTISED_LENGTH = 5
ADVERTISED_LOCKED = 0x02  # lost/stolen lock, set through the SOUNDBOKS cloud
# From this firmware on the serial channel carries pw_rpc instead of the commands below
FIRST_RPC_FIRMWARE = (3, 0, 0)

# Serial replies: 01, the payload length as u32 big-endian, then the payload over as many
# notifications as it takes. These opcodes answer with one bare packet instead.
FRAME_START = 0x01
FRAME_HEADER_LENGTH = 5
UNFRAMED_OPCODES = frozenset({"fwv", "pmv", "ski", "gdi", "tpg"})
# An error answer is 02 and a code, e.g. 02 29 or 02 06
ERROR_START = 0x02
ERROR_LENGTH = 2


class SoundProfile(IntEnum):
    """The profile mode byte; band bytes only apply to CUSTOM."""

    POWER = 0
    BASS_PLUS = 1
    INDOOR = 2
    CUSTOM = 3


class ProductModel(IntEnum):
    """The model byte of the advertisement; old SB3 units send 0."""

    SB3 = 1
    SB75 = 2
    SB4 = 3
    SB5 = 4
    SB6 = 5
    SB7 = 6
    SB8 = 7
    LB1 = 124


MODEL_NAMES: dict[int, str] = {
    ProductModel.SB3: "SOUNDBOKS (Gen. 3)",
    ProductModel.SB75: "SOUNDBOKS Go",
    ProductModel.SB4: "SOUNDBOKS (Gen. 4)",
}


class StereoRole(IntEnum):
    MONO = 0
    LEFT = 1
    RIGHT = 2


class Input(IntFlag):
    """Audio inputs currently playing."""

    CHANNEL_1 = 1
    CHANNEL_2 = 2
    AUX = 4
    BLUETOOTH = 8
    TEAMUP = 16


@dataclass(frozen=True)
class SpeakerState:
    volume: int  # raw, 0-255
    profile: SoundProfile
    stereo: StereoRole
    inputs: Input
    # The stored custom EQ in dB, lowest band first; it only sounds in the CUSTOM profile
    eq: tuple[int, ...] = FLAT_EQ


@dataclass(frozen=True)
class Advertisement:
    """What the speaker tells about itself without a connection."""

    model: int
    firmware: tuple[int, int, int]
    pin_enabled: bool
    locked: bool

    @property
    def firmware_version(self) -> str:
        return ".".join(map(str, self.firmware))

    @property
    def model_name(self) -> str:
        return MODEL_NAMES.get(self.model, f"SOUNDBOKS (model {self.model})")

    @property
    def speaks_serial(self) -> bool:
        """False for firmware that replaced the serial protocol with pw_rpc."""
        return self.firmware < FIRST_RPC_FIRMWARE


class SoundboksError(Exception):
    """Base for every error talking to the speaker."""


class SpeakerUnreachableError(SoundboksError):
    """No connection: the speaker is off, out of range, or another app holds it."""


class PinRequiredError(SoundboksError):
    """The speaker has a PIN and none was given."""


class InvalidPinError(SoundboksError):
    """The speaker refused the PIN."""


class UnsupportedSpeakerError(SoundboksError):
    """The speaker lacks the characteristics this integration needs."""


class RebootIncompleteError(SoundboksError):
    """Auto-boot was armed but the power-off didn't follow: the next power-off restarts."""


class ProtocolError(SoundboksError):
    """The speaker sent bytes that don't match the expected layout."""


class CommandRefusedError(SoundboksError):
    """The speaker answered a serial command with an error code."""


def level_to_raw(level: float) -> int:
    """Home Assistant's 0.0-1.0 volume to the speaker's byte, clamped."""
    return round(min(max(level, 0.0), 1.0) * MAX_RAW_VOLUME)


def raw_to_level(raw: int) -> float:
    return raw / MAX_RAW_VOLUME


def _single_byte(data: bytes, what: str) -> int:
    if len(data) != 1:
        raise ProtocolError(f"{what}: expected 1 byte, got {data.hex() or 'nothing'}")
    return data[0]


def decode_volume(data: bytes) -> int:
    return _single_byte(data, "volume")


def _check_profile(data: bytes) -> None:
    if len(data) != PROFILE_LENGTH:
        raise ProtocolError(f"profile: expected {PROFILE_LENGTH} bytes, got {data.hex()}")


def decode_profile(data: bytes) -> SoundProfile:
    _check_profile(data)
    try:
        return SoundProfile(data[0])
    except ValueError:
        raise ProtocolError(f"profile: unknown mode {data[0]}") from None


def encode_profile(current: bytes, profile: SoundProfile) -> bytes:
    """Switch profile, keeping the custom EQ bands the speaker already stores.

    The band bytes go back as they are, except bands never set (0), which are written as flat:
    switching to CUSTOM then never sends an invalid band.
    """
    _check_profile(current)
    flat = _band_to_raw(0)
    return bytes([profile]) + bytes(flat if raw == EQ_UNSET else raw for raw in current[1:])


def decode_eq(data: bytes) -> tuple[int, ...]:
    """The custom EQ stored in a profile value, in dB."""
    _check_profile(data)
    return tuple(_raw_to_band(raw) for raw in data[1:])


def encode_custom_eq(bands: tuple[int, ...]) -> bytes:
    """A profile value selecting CUSTOM with these bands, in dB."""
    if len(bands) != EQ_BANDS:
        raise ValueError(f"expected {EQ_BANDS} EQ bands, got {len(bands)}")
    for band in bands:
        if not EQ_MIN_DB <= band <= EQ_MAX_DB:
            raise ValueError(f"EQ band {band} dB is outside {EQ_MIN_DB} to {EQ_MAX_DB}")
    return bytes([SoundProfile.CUSTOM]) + bytes(_band_to_raw(band) for band in bands)


def _band_to_raw(db: int) -> int:
    return (db - EQ_MIN_DB) * 5 + 1


def _raw_to_band(raw: int) -> int:
    if raw == EQ_UNSET:
        return 0
    return min(max(round((raw - 1) / 5) + EQ_MIN_DB, EQ_MIN_DB), EQ_MAX_DB)


def decode_firmware(data: bytes) -> str:
    """The firmware version, as ASCII ("2.4.2") or as raw numbers between dots (02 2e 04 2e 03)."""
    text = data.rstrip(b"\x00 ")  # padding, in the ASCII form
    if text and all(part.isdigit() for part in text.split(b".")):
        return text.decode()
    parts = data.split(b".")
    if all(len(part) == 1 for part in parts):
        return ".".join(str(part[0]) for part in parts)  # a trailing 00 is a zero here
    return "".join(chr(b) if 0x20 <= b < 0x7F else str(b) for b in text)


def decode_stereo(data: bytes) -> StereoRole:
    value = _single_byte(data, "stereo role")
    try:
        return StereoRole(value)
    except ValueError:
        raise ProtocolError(f"stereo role: unknown value {value}") from None


def decode_inputs(data: bytes) -> Input:
    """The second byte of the sound status is the active inputs; unknown bits are ignored."""
    if len(data) < 2:
        raise ProtocolError(f"sound status: expected at least 2 bytes, got {data.hex()}")
    known = 0
    for member in Input:
        known |= member
    return Input(data[1] & known)


def pin_required(security: bytes) -> bool:
    return bool(security[:1] and security[0] & PIN_ENABLED)


def validate_pin(pin: str) -> str:
    """The PIN without surrounding spaces; ValueError unless it is exactly 4 digits."""
    pin = pin.strip()
    if len(pin) != PIN_LENGTH or not pin.isascii() or not pin.isdigit():
        raise ValueError(f"a PIN is {PIN_LENGTH} digits")
    return pin


def auth_command(pin: str) -> bytes:
    return b"aup" + validate_pin(pin).encode()


def auth_accepted(packet: bytes) -> bool:
    """The speaker's answer to auth_command: True accepted, False refused."""
    if packet == AUTH_ACCEPTED:
        return True
    if packet[:1] and packet[0] in ERROR_RESPONSES:
        return False
    raise ProtocolError(f"unexpected answer to the PIN: {packet.hex()}")


def parse_advertisement(manufacturer_data: Mapping[int, bytes]) -> Advertisement | None:
    """The speaker's advertised model, firmware and security; None if it doesn't send them."""
    data = manufacturer_data.get(MANUFACTURER_ID)
    if data is None or len(data) < ADVERTISED_LENGTH:
        return None
    security, major, minor, patch, model = data[:ADVERTISED_LENGTH]
    return Advertisement(
        model=model or ProductModel.SB3,
        firmware=(major, minor, patch),
        pin_enabled=bool(security & PIN_ENABLED),
        locked=bool(security & ADVERTISED_LOCKED),
    )


class SerialReply:
    """Reassembles the speaker's answer to one serial command from RX notifications."""

    def __init__(self, opcode: str) -> None:
        self._framed = opcode not in UNFRAMED_OPCODES
        self._length: int | None = None
        self._payload = b""

    def feed(self, packet: bytes) -> bytes | None:
        """Add a notification; the payload once complete, else None.

        Raises CommandRefusedError for an error code, ProtocolError for anything malformed.
        """
        if self._length is None:
            return self._first(packet)
        self._payload += packet
        return self._complete()

    def _first(self, packet: bytes) -> bytes | None:
        # Only as long as an error: a Go's fwv answer, 02 2e 04 2e 03, starts with 02 too
        if len(packet) == ERROR_LENGTH and packet[0] == ERROR_START:
            raise CommandRefusedError(f"error {packet[1:].hex()}")
        if not self._framed:
            return packet
        if len(packet) < FRAME_HEADER_LENGTH or packet[0] != FRAME_START:
            raise ProtocolError(f"serial reply: expected a frame header, got {packet.hex()}")
        self._length = int.from_bytes(packet[1:FRAME_HEADER_LENGTH])
        self._payload = packet[FRAME_HEADER_LENGTH:]
        return self._complete()

    def _complete(self) -> bytes | None:
        assert self._length is not None
        if len(self._payload) < self._length:
            return None
        if len(self._payload) > self._length:
            raise ProtocolError(
                f"serial reply: {len(self._payload)} bytes for a {self._length}-byte frame"
            )
        return self._payload
