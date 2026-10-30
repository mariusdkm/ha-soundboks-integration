"""Base entity: one device per speaker."""

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import SoundboksCoordinator
from .protocol import Input


class SoundboksEntity(CoordinatorEntity[SoundboksCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SoundboksCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer=MANUFACTURER,
            model=(
                coordinator.advertisement.model_name
                if coordinator.advertisement
                else "SOUNDBOKS (BLE)"
            ),
            name=coordinator.config_entry.title,
            sw_version=coordinator.firmware,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.present and self.coordinator.data is not None


INPUT_NAMES = {
    Input.CHANNEL_1: "Channel 1",
    Input.CHANNEL_2: "Channel 2",
    Input.AUX: "AUX",
    Input.BLUETOOTH: "Bluetooth",
    Input.TEAMUP: "TeamUp",
}


def describe_inputs(inputs: Input) -> str | None:
    """Active inputs as "AUX, Bluetooth", None when nothing plays."""
    names = [name for flag, name in INPUT_NAMES.items() if flag in inputs]
    return ", ".join(names) or None
