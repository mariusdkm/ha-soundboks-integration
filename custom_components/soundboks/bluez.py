"""BlueZ workarounds for speakers that also do classic Bluetooth audio.

The SOUNDBOKS Go is dual-mode: it is an A2DP sink as well as a BLE peripheral. For such a device
BlueZ picks the bearer by itself, and it may connect over BR/EDR, where the speaker offers only a
stub of its GATT table. BlueZ then keeps that stub as the device's services, even for later LE
connections. Phones don't hit this because their apps ask for an LE connection explicitly.

Both helpers act only on devices behind a local BlueZ adapter (they need the device's D-Bus path)
and are best effort: on anything else, an ESPHome proxy (LE only anyway) or an older BlueZ, they
do nothing.
"""

import logging

from bleak.backends.device import BLEDevice
from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

_LOGGER = logging.getLogger(__name__)

BLUEZ = "org.bluez"
DEVICE_INTERFACE = "org.bluez.Device1"
ADAPTER_INTERFACE = "org.bluez.Adapter1"


def bluez_path(device: BLEDevice) -> str | None:
    """The device's BlueZ object path, None when it isn't behind a local BlueZ adapter."""
    details = device.details
    path = details.get("path") if isinstance(details, dict) else None
    return path if isinstance(path, str) and path.startswith("/org/bluez/") else None


async def prefer_le(device: BLEDevice) -> None:
    """Make BlueZ connect over LE (Device1.PreferredBearer, BlueZ 5.81+).

    Set before every connection: BlueZ forgets it along with the device object, which it drops
    a few minutes after the speaker turns off.
    """
    if (path := bluez_path(device)) is None:
        return
    error = await _call(
        path,
        "org.freedesktop.DBus.Properties",
        "Set",
        "ssv",
        [DEVICE_INTERFACE, "PreferredBearer", Variant("s", "le")],
    )
    if error:
        _LOGGER.debug("Could not prefer LE for %s: %s", device.address, error)


async def forget_device(device: BLEDevice) -> bool:
    """Drop BlueZ's device object, and with it a stale GATT table; True if it was removed.

    Only for an unpaired device: BlueZ creates it afresh from its next advertisement. Removing a
    paired one would delete its pairing too, e.g. a Go the host streams audio to.
    """
    if (path := bluez_path(device)) is None:
        return False
    properties = await _device_properties(path)
    if properties is None or properties.get("Paired") or properties.get("Bonded"):
        _LOGGER.debug("Not removing %s from BlueZ: paired, or unknown", device.address)
        return False
    adapter = path.rsplit("/", 1)[0]
    error = await _call(adapter, ADAPTER_INTERFACE, "RemoveDevice", "o", [path])
    if error:
        _LOGGER.debug("Could not remove %s from BlueZ: %s", device.address, error)
        return False
    return True


async def _device_properties(path: str) -> dict[str, object] | None:
    """Device1's properties as plain values, None if they couldn't be read."""
    reply = await _request(
        path, "org.freedesktop.DBus.Properties", "GetAll", "s", [DEVICE_INTERFACE]
    )
    if isinstance(reply, str) or not reply.body:
        return None
    return {name: variant.value for name, variant in reply.body[0].items()}


async def _call(path: str, interface: str, member: str, signature: str, body: list) -> str | None:
    """One method call on the system bus; the error, or None when it worked."""
    reply = await _request(path, interface, member, signature, body)
    return reply if isinstance(reply, str) else None


async def _request(
    path: str, interface: str, member: str, signature: str, body: list
) -> Message | str:
    """One method call on the system bus; its reply, or the error as text."""
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=False).connect()
    except Exception as err:  # noqa: BLE001 - no bus (not Linux, no permission): nothing to fix
        return f"no system bus ({err})"
    try:
        reply = await bus.call(
            Message(
                destination=BLUEZ,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body,
            )
        )
    except Exception as err:  # noqa: BLE001 - best effort, must never block a connection
        return str(err)
    finally:
        bus.disconnect()
    if reply is None:
        return "no reply"
    if reply.message_type == MessageType.ERROR:
        return f"{reply.error_name}: {reply.body}"
    return reply
