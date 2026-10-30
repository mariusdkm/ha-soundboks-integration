"""Diagnostics download, with the PIN redacted, plus what the speaker reports about itself.

The speaker is asked over its serial channel, read-only commands only. Left out on purpose: the
settings (gu2, which holds the Bluetooth password), the signed serial number (gsn) and the cloud
identifiers (ski, gdi). The protobuf replies are included as hex.
"""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_PIN
from .coordinator import SoundboksConfigEntry, SoundboksCoordinator
from .protocol import SoundboksError, decode_firmware

# Most widely supported first: the query stops at the first command left unanswered
DIAGNOSTIC_OPCODES = (
    "fwv",  # firmware version
    "pmv",  # product codename
    "gst",  # update-only firmware / tweeter calibration flags
    "gdc",  # SBDiagnostics protobuf: failure flags
    "gla",  # amount of stored log
    "gcm",  # UpdateManifest protobuf
    "gss",  # firmware git SHA, short
    "gsl",  # firmware git SHA, long
    "gma",  # Bluetooth MAC, little-endian
    "agv",  # ATtiny firmware protobuf (SB75 and SB4 only)
    "agt",  # ATtiny temperature protobuf, unit unknown (SB75 and SB4 only)
)
TEXT_OPCODES = frozenset({"pmv", "gss", "gsl"})


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SoundboksConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    advertisement = coordinator.advertisement
    return {
        "entry": async_redact_data(dict(entry.data), {CONF_PIN}),
        "options": dict(entry.options),
        "present": coordinator.present,
        "firmware": coordinator.firmware,
        "advertisement": asdict(advertisement) if advertisement is not None else None,
        "state": asdict(coordinator.data) if coordinator.data is not None else None,
        "speaker": await _async_query(coordinator),
    }


async def _async_query(coordinator: SoundboksCoordinator) -> dict[str, Any] | str:
    try:
        replies = await coordinator.async_query(DIAGNOSTIC_OPCODES)
    except SoundboksError as err:
        return f"not read: {err}"
    return {opcode: _describe(opcode, reply) for opcode, reply in replies.items()}


def _describe(opcode: str, reply: bytes | str) -> Any:
    if isinstance(reply, str):
        return reply  # what went wrong
    described: dict[str, Any] = {"hex": reply.hex()}
    if opcode == "fwv":
        described["version"] = decode_firmware(reply)
    elif opcode in TEXT_OPCODES:
        described["text"] = reply.decode("ascii", "replace")
    elif opcode == "gma" and len(reply) >= 6:
        described["mac"] = ":".join(f"{b:02X}" for b in reversed(reply[:6]))
    return described
