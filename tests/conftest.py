"""Shared fixtures: a fake speaker standing in for the BLE stack."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.bluetooth import (
    MONOTONIC_TIME,
    BluetoothServiceInfoBleak,
    async_clear_advertisement_history,
    async_get_advertisement_callback,
)
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.soundboks.const import CONF_PIN, DOMAIN
from custom_components.soundboks.coordinator import SoundboksCoordinator
from custom_components.soundboks.protocol import (
    ADVERTISED_SERVICE_UUID,
    AUTO_BOOT_ONCE,
    FIRMWARE_UUID,
    INPUTS_UUID,
    POWER_UUID,
    PROFILE_UUID,
    SECURITY_UUID,
    SERIAL_RX_UUID,
    SERIAL_TX_UUID,
    STEREO_UUID,
    VOLUME_UUID,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
NAME = "#202047"
PLAYER = "media_player.soundboks_202047"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ in every test."""
    return


@pytest.fixture(autouse=True)
def no_reboot_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reboot retries wait between reconnects; tests don't need to."""
    monkeypatch.setattr("custom_components.soundboks.client.REBOOT_RETRY_DELAY", 0)
    monkeypatch.setattr("custom_components.soundboks.client.AUTO_BOOT_WAIT", 0.01)


def make_ble_device() -> BLEDevice:
    return BLEDevice(ADDRESS, NAME, None)


@dataclass
class FakeSpeaker:
    """The speaker's state, which outlives each connection to it."""

    values: dict[str, bytes] = field(
        default_factory=lambda: {
            VOLUME_UUID: b"\x2d",
            POWER_UUID: b"\x01",
            PROFILE_UUID: bytes([0, 51, 51, 51, 51, 51, 51]),
            STEREO_UUID: b"\x00",
            INPUTS_UUID: bytes([0, 8]),
            SECURITY_UUID: b"\x00",
            FIRMWARE_UUID: b"2.4.2",
        }
    )
    pin: str | None = None
    unreachable: bool = False
    drop_on_power_write: bool = True
    # Like the real speaker: the auto-boot write is never answered, and the link stays up
    hang_on_auto_boot: bool = False
    # Serial command -> the RX notifications answering it
    serial_replies: dict[bytes, list[bytes]] = field(default_factory=dict)
    failing_reads: set[str] = field(default_factory=set)
    # Writes refused with a GATT error, the link staying up
    failing_writes: set[str] = field(default_factory=set)
    # The serial channel's (TX, RX) UUIDs; some firmware declares the 16-bit forms
    serial: tuple[str, str] = (SERIAL_TX_UUID, SERIAL_RX_UUID)
    # Characteristics missing from the GATT table the next connections see
    hidden: set[str] = field(default_factory=set)
    # Connects that fail right after a power write, as a speaker restarting its radio would
    failing_connects_after_power_write: int = 0
    writes: list[tuple[str, bytes]] = field(default_factory=list)
    connect_count: int = 0
    failing_connects: int = 0
    client: FakeBleakClient | None = None
    clients: list[FakeBleakClient] = field(default_factory=list)

    def set_pin(self, pin: str) -> None:
        self.pin = pin
        self.values[SECURITY_UUID] = b"\x01"

    def notify(self, uuid: str, data: bytes) -> None:
        assert self.client is not None and self.client.is_connected
        self.client.callbacks[uuid](None, bytearray(data))


class FakeServices:
    def __init__(self, speaker: FakeSpeaker) -> None:
        self._uuids = (set(speaker.values) | set(speaker.serial)) - speaker.hidden

    def get_characteristic(self, uuid: str) -> object | None:
        return object() if uuid in self._uuids else None


class FakeBleakClient:
    """Enough of BleakClient for the integration's client."""

    def __init__(self, speaker: FakeSpeaker, disconnected_callback: Callable | None) -> None:
        self.speaker = speaker
        self.is_connected = True
        self.authenticated = speaker.pin is None
        self.callbacks: dict[str, Callable] = {}
        self.services = FakeServices(speaker)
        self._disconnected_callback = disconnected_callback

    def _check(self) -> None:
        if not self.is_connected:
            raise BleakError("not connected")

    async def read_gatt_char(self, uuid: str) -> bytearray:
        self._check()
        if uuid in self.speaker.failing_reads:
            raise BleakError("read failed")
        return bytearray(self.speaker.values[uuid])

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        await asyncio.sleep(0)  # a real write suspends, letting concurrent callers interleave
        self._check()
        data = bytes(data)
        self.speaker.writes.append((uuid, data))
        tx, rx = self.speaker.serial
        if uuid == tx:
            if data.startswith(b"aup"):
                accepted = data[3:].decode() == self.speaker.pin
                self.authenticated = self.authenticated or accepted
                answer = b"\x01\x00\x00\x00\x00" if accepted else b"\x02\x00"
                self.callbacks[rx](None, bytearray(answer))
            for packet in self.speaker.serial_replies.get(data, []):
                self.callbacks[rx](None, bytearray(packet))
            return
        if not self.authenticated:
            raise BleakError("ATT error 0x81")
        if uuid in self.speaker.failing_writes:
            raise BleakError("ATT error 0x03")
        if uuid == POWER_UUID and data == AUTO_BOOT_ONCE and self.speaker.hang_on_auto_boot:
            await asyncio.Event().wait()
        if uuid == POWER_UUID:
            self.speaker.failing_connects = self.speaker.failing_connects_after_power_write
        if uuid == POWER_UUID and self.speaker.drop_on_power_write:
            self.drop()
            raise BleakError("disconnected")
        self.speaker.values[uuid] = data

    async def start_notify(self, uuid: str, callback: Callable) -> None:
        self._check()
        self.callbacks[uuid] = callback

    async def disconnect(self) -> None:
        self.drop()

    def drop(self) -> None:
        if self.is_connected:
            self.is_connected = False
            if self._disconnected_callback:
                self._disconnected_callback(self)


@pytest.fixture
def fake_speaker() -> Iterator[FakeSpeaker]:
    speaker = FakeSpeaker()

    async def establish_connection(client_class, device, name, disconnected_callback=None, **_):
        if speaker.unreachable:
            raise BleakError("device not found")
        if speaker.failing_connects:
            speaker.failing_connects -= 1
            raise BleakError("device not found")
        speaker.connect_count += 1
        speaker.client = FakeBleakClient(speaker, disconnected_callback)
        speaker.clients.append(speaker.client)
        return speaker.client

    with patch("custom_components.soundboks.client.establish_connection", establish_connection):
        yield speaker
    for client in speaker.clients:
        client.drop()  # links left open end, and with them the clients' idle timers


def make_service_info(
    age: float = 0.0, manufacturer_data: dict[int, bytes] | None = None
) -> BluetoothServiceInfoBleak:
    """An advertisement heard age seconds ago."""
    return BluetoothServiceInfoBleak(
        name=f"AppControl {NAME}",
        address=ADDRESS,
        rssi=-60,
        manufacturer_data=manufacturer_data or {},
        service_data={},
        service_uuids=[ADVERTISED_SERVICE_UUID],
        source="local",
        device=make_ble_device(),
        advertisement=None,
        connectable=True,
        time=MONOTONIC_TIME() - age,
        tx_power=None,
    )


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    return make_entry()


def make_entry(pin: str | None = None, **options: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title=f"SOUNDBOKS {NAME}",
        data={CONF_ADDRESS: ADDRESS} | ({CONF_PIN: pin} if pin else {}),
        options=options,
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    with patch(
        "custom_components.soundboks.async_ble_device_from_address",
        return_value=make_ble_device(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def advertise(
    hass: HomeAssistant, entry: MockConfigEntry, manufacturer_data: dict[int, bytes] | None = None
) -> None:
    """The speaker shows up, through HA's Bluetooth manager like a real advertisement.

    Going through the manager matters: it keeps the advertisement history the integration
    reads, and skips integration callbacks for repeated identical advertisements.
    """
    async_get_advertisement_callback(hass)(make_service_info(manufacturer_data=manufacturer_data))
    await hass.async_block_till_done()


def go_unavailable(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """HA stopped hearing the speaker: it drops the history first, then calls back."""
    async_clear_advertisement_history(hass, ADDRESS)
    entry.runtime_data.async_handle_unavailable(make_service_info())


async def setup_present(
    hass: HomeAssistant, entry: MockConfigEntry, manufacturer_data: dict[int, bytes] | None = None
) -> SoundboksCoordinator:
    """Set up with the speaker on and advertising."""
    await setup_entry(hass, entry)
    await advertise(hass, entry, manufacturer_data)
    return entry.runtime_data


@pytest.fixture
async def coordinator(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
) -> AsyncIterator[SoundboksCoordinator]:
    """The default entry, set up with the speaker on; for tests that change nothing first."""
    if "freezer" in request.fixturenames:
        request.getfixturevalue("freezer")  # freeze first: adverts from before it look stale
    yield await setup_present(hass, mock_entry)


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta | float
) -> None:
    """Let time pass (a timedelta, or seconds), running what was scheduled for it."""
    freezer.tick(delta if isinstance(delta, timedelta) else timedelta(seconds=delta))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def call_player(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        MP_DOMAIN, service, {ATTR_ENTITY_ID: PLAYER, **data}, blocking=True
    )


def power_writes(speaker: FakeSpeaker) -> list[bytes]:
    return [data for uuid, data in speaker.writes if uuid == POWER_UUID]
