"""Whether Home Assistant can reach the speaker right now."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SoundboksConfigEntry, SoundboksCoordinator
from .entity import SoundboksEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SoundboksConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([Connection(entry.runtime_data)])


class Connection(SoundboksEntity, BinarySensorEntity):
    """On while commands are worth trying: the speaker was heard just now, or the link is open.

    Off means it's off, out of range, or another app holds it.
    """

    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SoundboksCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_connection"

    @property
    def available(self) -> bool:
        # Off is the answer when the speaker is away, not unavailable
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.present
