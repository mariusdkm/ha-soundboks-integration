"""The BLE connection to one speaker.

The speaker takes a single connection at a time and stops advertising while it has one. By default
the client never holds the link for long: a command keeps it for an idle window (a burst of volume
changes reuses it), anything else that opened the link closes it straight away. With no idle
timeout it keeps the link instead, for as long as the speaker does. One lock runs every operation
in turn.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import monotonic

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    clear_cache,
    close_stale_connections,
    establish_connection,
    get_connected_devices,
)

from .bluez import bluez_path, forget_device, prefer_le, removable
from .protocol import (
    AUTO_BOOT_ONCE,
    FIRMWARE_UUID,
    INPUTS_UUID,
    MAX_RAW_VOLUME,
    POWER_OFF,
    POWER_UUID,
    PROFILE_UUID,
    REQUIRED_UUIDS,
    SECURITY_UUID,
    SERIAL_RX_UUIDS,
    SERIAL_TX_UUIDS,
    STEREO_UUID,
    VOLUME_UUID,
    CommandRefusedError,
    Input,
    InvalidPinError,
    PinRequiredError,
    ProtocolError,
    RebootIncompleteError,
    SerialReply,
    SoundboksError,
    SoundProfile,
    SpeakerState,
    SpeakerUnreachableError,
    StereoRole,
    UnsupportedSpeakerError,
    auth_accepted,
    auth_command,
    decode_eq,
    decode_firmware,
    decode_inputs,
    decode_log_amount,
    decode_profile,
    decode_stereo,
    decode_volume,
    encode_custom_eq,
    encode_profile,
    pin_required,
)

_LOGGER = logging.getLogger(__name__)

UNREACHABLE_MESSAGE = (
    "Could not reach the speaker: it may be off, out of range, or another app may be connected"
)
CONNECT_ATTEMPTS = 2  # fail fast: the next poll retries anyway
RESPONSE_TIMEOUT = 5.0
# Serial command reading the amount of stored log, sampled with every poll
LOG_AMOUNT_OPCODE = "gla"
# The speaker never answers the auto-boot write, so the client waits this long for it to reach
# the speaker, then moves on (see AUTO_BOOT_ONCE)
AUTO_BOOT_WAIT = 3.0
# Reconnecting right after a power write often fails, so the power-off that completes a reboot is
# retried for a while
REBOOT_ATTEMPTS = 5
REBOOT_RETRY_DELAY = 3.0
LINK_ERRORS = (BleakError, TimeoutError)
# A table read while the speaker boots can be partial too: clearing it is retried this often
STALE_TABLE_RETRY = 60.0


class SoundboksClient:
    def __init__(
        self,
        ble_device: BLEDevice | None,
        pin: str | None,
        *,
        idle_timeout: float | None = 30.0,
        on_volume: Callable[[int], None] | None = None,
        on_inputs: Callable[[Input], None] | None = None,
        on_link_lost: Callable[[], None] | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._pin = pin
        # None: never hang up, keep every link until the speaker drops it
        self._idle_timeout = idle_timeout
        self._on_volume = on_volume
        self._on_inputs = on_inputs
        # Called when a link drops by itself (not when the client closes it)
        self._on_link_lost = on_link_lost
        self._lock = asyncio.Lock()
        self._client: BleakClient | None = None
        self._authenticated = False
        self._responses: asyncio.Queue[bytes] = asyncio.Queue()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task | None = None
        # The serial channel's UUIDs as this speaker declares them, found at each connection
        self._serial_tx: str | None = None
        # When a bad GATT table was last cleared; missing characteristics soon after are real
        self._stale_table_cleared_at: float | None = None

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """The latest device seen advertising, used for the next connection."""
        self._ble_device = ble_device

    @property
    def keeps_link(self) -> bool:
        return self._idle_timeout is None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def close_orphaned_link(self) -> bool:
        """Close a link BlueZ holds to the speaker that isn't ours; True if there was one.

        Such a link happens when a connection attempt is abandoned (it timed out, e.g. while the
        speaker was off) but BlueZ completes it later. The speaker then stops advertising and
        refuses everyone else, while nobody uses the link: without this, it stays unreachable
        until BlueZ or the speaker restarts.
        """
        async with self._lock:
            if self.is_connected or self._ble_device is None:
                return False
            if not await get_connected_devices(self._ble_device):
                return False
            _LOGGER.warning(
                "Closing a Bluetooth link to %s that Home Assistant didn't own; it blocked"
                " every other connection",
                self._ble_device.address,
            )
            await close_stale_connections(self._ble_device)
            return True

    async def check_supported(self) -> None:
        async def check(client: BleakClient) -> None:
            if self._missing_required(client):
                raise UnsupportedSpeakerError("this speaker model is not supported")

        await self._run(check, command=False, authenticate=False)

    async def pin_required(self) -> bool:
        async def read(client: BleakClient) -> bool:
            return pin_required(await self._read(client, SECURITY_UUID))

        return await self._run(read, command=False, authenticate=False)

    async def verify_pin(self, pin: str) -> None:
        """Authenticate with pin; on success it becomes the client's PIN."""

        async def verify(client: BleakClient) -> None:
            await self._authenticate(client, pin)
            self._pin = pin

        await self._run(verify, command=True, authenticate=False)

    async def read_firmware(self) -> str:
        async def read(client: BleakClient) -> str:
            return decode_firmware(await self._read(client, FIRMWARE_UUID))

        return await self._run(read, command=False, authenticate=False)

    async def read_state(self) -> SpeakerState:
        async def read(client: BleakClient) -> SpeakerState:
            profile = await self._read(client, PROFILE_UUID)
            return SpeakerState(
                volume=decode_volume(await self._read(client, VOLUME_UUID)),
                profile=decode_profile(profile),
                stereo=decode_stereo(await self._read(client, STEREO_UUID)),
                inputs=decode_inputs(await self._read(client, INPUTS_UUID)),
                eq=decode_eq(profile),
                log_amount=await self._read_log_amount(client),
            )

        return await self._run(read, command=False)

    async def _read_log_amount(self, client: BleakClient) -> int | None:
        """Best effort, on the poll's own link: a failure here must not cost the state read.

        A dropped link still raises, failing the poll like any other read would.
        """
        if self._serial_tx is None:
            return None
        try:
            raw = await self._serial_request(client, LOG_AMOUNT_OPCODE)
            amount = decode_log_amount(raw)
        except (TimeoutError, SoundboksError) as err:
            _LOGGER.debug("Could not read the log amount: %s", str(err) or "timed out")
            return None
        _LOGGER.debug("Log amount %d (raw %s)", amount, raw.hex())
        return amount

    async def set_volume(self, raw: int) -> None:
        if not 0 <= raw <= MAX_RAW_VOLUME:
            raise ValueError(f"volume {raw} is outside 0-{MAX_RAW_VOLUME}")
        await self._write(VOLUME_UUID, bytes([raw]))

    async def set_profile(self, profile: SoundProfile) -> None:
        async def write(client: BleakClient) -> None:
            current = await self._read(client, PROFILE_UUID)
            await client.write_gatt_char(
                PROFILE_UUID, encode_profile(current, profile), response=True
            )

        await self._run(write, command=True)

    async def set_custom_eq(self, bands: tuple[int, ...]) -> None:
        """Select the CUSTOM profile with these bands, in dB."""
        await self._write(PROFILE_UUID, encode_custom_eq(bands))

    async def set_stereo(self, role: StereoRole) -> None:
        await self._write(STEREO_UUID, bytes([role]))

    async def power_off(self) -> None:
        await self._run(self._power_write(POWER_OFF), command=False)

    async def arm_auto_boot(self) -> None:
        """Make the speaker boot by itself the next time it gets power, once.

        Cutting its power afterwards turns it off in a way that restoring the power undoes. The
        speaker never answers this write, so after a short wait it counts as done.
        """

        async def arm(client: BleakClient) -> None:
            try:
                async with asyncio.timeout(AUTO_BOOT_WAIT):
                    await client.write_gatt_char(POWER_UUID, AUTO_BOOT_ONCE, response=True)
            except TimeoutError:
                _LOGGER.debug("No answer to the auto-boot write, as expected")
            except BleakError:
                if client.is_connected:
                    raise  # refused, not dropped: nothing is armed
                # Some firmware drops the link instead of answering, like after a power-off
                _LOGGER.debug("Link dropped after the auto-boot write")
            # The write is still pending in BlueZ: nothing else can use this link
            await self._drop()

        await self._run(arm, command=False)

    async def reboot(self) -> None:
        """Auto-boot once, then power off: the speaker restarts by itself."""
        await self.arm_auto_boot()
        for _ in range(REBOOT_ATTEMPTS):
            await asyncio.sleep(REBOOT_RETRY_DELAY)
            try:
                await self.power_off()
            except SpeakerUnreachableError:
                _LOGGER.debug("Reconnect for the reboot's power-off failed, retrying")
            else:
                return
        raise RebootIncompleteError("the speaker will restart at its next power-off instead of now")

    async def query(self, opcodes: tuple[str, ...]) -> dict[str, bytes | str]:
        """Send read-only serial commands in turn: each one's reply payload, or what went wrong.

        Stops at the first command without a complete answer: a late reply would be taken for
        the next command's.
        """

        async def run(client: BleakClient) -> dict[str, bytes | str]:
            results: dict[str, bytes | str] = {}
            for opcode in opcodes:
                try:
                    results[opcode] = await self._serial_request(client, opcode)
                except CommandRefusedError as err:
                    results[opcode] = f"refused: {err}"
                except (TimeoutError, ProtocolError) as err:
                    results[opcode] = f"no valid answer: {str(err) or 'timed out'}"
                    break
            return results

        return await self._run(run, command=False)

    async def disconnect(self) -> None:
        async with self._lock:
            await self._drop()

    def _power_write(self, data: bytes) -> Callable[[BleakClient], Awaitable[None]]:
        async def write(client: BleakClient) -> None:
            # The speaker never answers a power write: it drops the link, which means it worked
            try:
                await client.write_gatt_char(POWER_UUID, data, response=True)
            except LINK_ERRORS:
                _LOGGER.debug("Link dropped after power write %s, as expected", data.hex())
            await self._drop()

        return write

    async def _write(self, uuid: str, data: bytes) -> None:
        async def write(client: BleakClient) -> None:
            await client.write_gatt_char(uuid, data, response=True)

        await self._run(write, command=True)

    async def _run[T](
        self,
        operation: Callable[[BleakClient], Awaitable[T]],
        *,
        command: bool,
        authenticate: bool = True,
    ) -> T:
        """Run operation on the link, connecting first if needed.

        A command (re)starts the idle window. Anything else closes a link it opened, and leaves
        the idle window of an existing one alone, so polls never keep the speaker busy. Without
        an idle timeout every link stays open.
        """
        async with self._lock:
            opened = not self.is_connected
            if command:
                self._cancel_idle()
            try:
                client = await self._connect()
                if authenticate:
                    await self._ensure_authenticated(client)
                result = await operation(client)
            except LINK_ERRORS as err:
                await self._drop()
                raise SpeakerUnreachableError(UNREACHABLE_MESSAGE) from err
            except BaseException:
                await self._drop()
                raise
            if command and self.is_connected:
                self._arm_idle()
            elif opened and not self.keeps_link:
                await self._drop()
            return result

    async def _connect(self) -> BleakClient:
        if self._client is not None and self._client.is_connected:
            return self._client
        if self._ble_device is None:
            raise SpeakerUnreachableError(UNREACHABLE_MESSAGE)
        # A dual-mode speaker (the Go) has no control services over BR/EDR: see bluez.py
        await prefer_le(self._ble_device)
        # A link we don't own would make this connection fail: see close_orphaned_link
        await close_stale_connections(self._ble_device)
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.name or self._ble_device.address,
                disconnected_callback=self._on_disconnected,
                max_attempts=CONNECT_ATTEMPTS,
            )
        except LINK_ERRORS as err:
            raise SpeakerUnreachableError(UNREACHABLE_MESSAGE) from err
        self._client = client
        self._authenticated = False
        self._responses = asyncio.Queue()
        if missing := self._missing_required(client):
            _LOGGER.debug("%s's GATT table lacks %s", self._ble_device.address, missing)
            if await self._clear_stale_table():
                raise SpeakerUnreachableError(UNREACHABLE_MESSAGE)
        else:
            # A bad table that comes back later (BlueZ recreated the device) is cleared at once
            self._stale_table_cleared_at = None
        self._serial_tx = _first_present(client, SERIAL_TX_UUIDS)
        # Keep this subscription for the link's lifetime: unsubscribing drops the authentication
        if serial_rx := _first_present(client, SERIAL_RX_UUIDS):
            await client.start_notify(serial_rx, self._on_response)
        if self._on_volume is not None:
            await client.start_notify(VOLUME_UUID, self._on_volume_notification)
        if self._on_inputs is not None and client.services.get_characteristic(INPUTS_UUID):
            try:
                await client.start_notify(INPUTS_UUID, self._on_inputs_notification)
            except BleakError:
                # Polls still read the inputs; only the live updates are lost
                _LOGGER.debug("Could not subscribe to the sound status", exc_info=True)
        return client

    @staticmethod
    def _missing_required(client: BleakClient) -> list[str]:
        return [uuid for uuid in REQUIRED_UUIDS if client.services.get_characteristic(uuid) is None]

    async def _clear_stale_table(self) -> bool:
        """Forget the speaker in BlueZ, in case its GATT table is bad; at most once a while.

        A table can be a BR/EDR leftover, or partial because it was read while the speaker
        booted. True when it cleared it: the link is dropped, and the next connection reads the
        table afresh. Only for an unpaired speaker behind a local BlueZ adapter; elsewhere the
        table is what it is.
        """
        device = self._ble_device
        if device is None or bluez_path(device) is None:
            return False
        now = monotonic()
        last = self._stale_table_cleared_at
        if last is not None and now - last < STALE_TABLE_RETRY:
            return False
        self._stale_table_cleared_at = now
        # Checked first: clearing bleak-retry-connector's copy removes the device from BlueZ
        # without looking, and removing a paired device deletes its pairing
        if not await removable(device):
            _LOGGER.debug("Not clearing %s's GATT table: paired, or unknown", device.address)
            return False
        await self._drop()
        if not await clear_cache(device.address):
            # bleak-retry-connector had no copy, so nothing removed BlueZ's yet
            await forget_device(device)
        _LOGGER.warning(
            "%s lacked its control characteristics: a table cached from a classic Bluetooth "
            "connection, or read while it started. Cleared it, the next connection reads it again",
            device.address,
        )
        return True

    async def _ensure_authenticated(self, client: BleakClient) -> None:
        if self._authenticated:
            return
        if pin_required(await self._read(client, SECURITY_UUID)):
            if not self._pin:
                raise PinRequiredError("the speaker has a PIN")
            await self._authenticate(client, self._pin)
        self._authenticated = True

    async def _authenticate(self, client: BleakClient, pin: str) -> None:
        if self._serial_tx is None:
            raise UnsupportedSpeakerError("the speaker has a PIN but no serial channel to send it")
        self._discard_responses()
        await client.write_gatt_char(self._serial_tx, auth_command(pin), response=True)
        answer = await asyncio.wait_for(self._responses.get(), RESPONSE_TIMEOUT)
        if not auth_accepted(answer):
            raise InvalidPinError("the speaker refused the PIN")
        self._authenticated = True

    async def _serial_request(self, client: BleakClient, opcode: str) -> bytes:
        if self._serial_tx is None:
            raise UnsupportedSpeakerError("the speaker has no serial channel")
        self._discard_responses()
        reply = SerialReply(opcode)
        await client.write_gatt_char(self._serial_tx, opcode.encode(), response=True)
        async with asyncio.timeout(RESPONSE_TIMEOUT):
            while (payload := reply.feed(await self._responses.get())) is None:
                pass
        return payload

    def _discard_responses(self) -> None:
        while not self._responses.empty():
            self._responses.get_nowait()

    @staticmethod
    async def _read(client: BleakClient, uuid: str) -> bytes:
        return bytes(await client.read_gatt_char(uuid))

    def _on_response(self, _sender: object, data: bytearray) -> None:
        self._responses.put_nowait(bytes(data))

    def _on_volume_notification(self, _sender: object, data: bytearray) -> None:
        try:
            raw = decode_volume(bytes(data))
        except ProtocolError:
            _LOGGER.debug("Ignoring volume notification %s", bytes(data).hex())
            return
        if self._on_volume is not None:
            self._on_volume(raw)

    def _on_inputs_notification(self, _sender: object, data: bytearray) -> None:
        try:
            inputs = decode_inputs(bytes(data))
        except ProtocolError:
            _LOGGER.debug("Ignoring sound status notification %s", bytes(data).hex())
            return
        if self._on_inputs is not None:
            self._on_inputs(inputs)

    def _on_disconnected(self, client: BleakClient) -> None:
        if client is self._client:
            self._client = None
            self._cancel_idle()
            _LOGGER.debug("The link to the speaker dropped")
            if self._on_link_lost is not None:
                self._on_link_lost()

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self._idle_timeout is None:
            return  # the link stays
        loop = asyncio.get_running_loop()
        self._idle_handle = loop.call_later(self._idle_timeout, self._idle_expired)

    def _cancel_idle(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _idle_expired(self) -> None:
        self._idle_handle = None
        self._idle_task = asyncio.create_task(self.disconnect())

    async def _drop(self) -> None:
        self._cancel_idle()
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except LINK_ERRORS:
                _LOGGER.debug("Error while disconnecting", exc_info=True)


def _first_present(client: BleakClient, uuids: tuple[str, ...]) -> str | None:
    return next((uuid for uuid in uuids if client.services.get_characteristic(uuid)), None)
