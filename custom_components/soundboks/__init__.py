"""SOUNDBOKS speakers over Bluetooth LE."""

from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    async_ble_device_from_address,
    async_register_callback,
    async_track_unavailable,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .client import SoundboksClient
from .const import CONF_IDLE_TIMEOUT, CONF_PIN, DOMAIN, IDLE_TIMEOUT, PLATFORMS
from .coordinator import SoundboksConfigEntry, SoundboksCoordinator, firmware_issue_id


async def async_setup_entry(hass: HomeAssistant, entry: SoundboksConfigEntry) -> bool:
    address = entry.data[CONF_ADDRESS]
    idle_timeout = float(entry.options.get(CONF_IDLE_TIMEOUT, IDLE_TIMEOUT))
    client = SoundboksClient(
        # None while the speaker is off: the first advertisement provides it
        async_ble_device_from_address(hass, address, connectable=True),
        entry.data.get(CONF_PIN),
        # 0 in the options means stay connected
        idle_timeout=idle_timeout or None,
        on_volume=lambda raw: coordinator.async_handle_volume(raw),
        on_inputs=lambda inputs: coordinator.async_handle_inputs(inputs),
        on_link_lost=lambda: coordinator.async_handle_link_lost(),
    )
    coordinator = SoundboksCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    entry.async_on_unload(
        async_register_callback(
            hass,
            coordinator.async_handle_advertisement,
            BluetoothCallbackMatcher(address=address, connectable=True),
            BluetoothScanningMode.PASSIVE,
        )
    )
    entry.async_on_unload(
        async_track_unavailable(hass, coordinator.async_handle_unavailable, address)
    )
    # Not async_config_entry_first_refresh: an off speaker must not hold up setup
    await coordinator.async_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SoundboksConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
        await entry.runtime_data.client.disconnect()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SoundboksConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, firmware_issue_id(entry.data[CONF_ADDRESS]))
