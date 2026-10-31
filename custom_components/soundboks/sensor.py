"""Which inputs are playing, the stored log amount, and the battery level when configured."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_BATTERY_ENTITY
from .coordinator import SoundboksConfigEntry, SoundboksCoordinator
from .entity import SoundboksEntity, describe_inputs


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SoundboksConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[SensorEntity] = [
        ActiveInputsSensor(entry.runtime_data),
        LogAmountSensor(entry.runtime_data),
    ]
    if source := entry.options.get(CONF_BATTERY_ENTITY):
        entities.append(BatterySensor(entry.runtime_data, source))
    async_add_entities(entities)


class ActiveInputsSensor(SoundboksEntity, SensorEntity):
    _attr_translation_key = "active_inputs"

    def __init__(self, coordinator: SoundboksCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_active_inputs"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return describe_inputs(self.coordinator.data.inputs) or "None"


class LogAmountSensor(SoundboksEntity, SensorEntity):
    """The speaker's stored log amount (gla), sampled with every poll; unit unknown.

    Its history shows whether the log fills up before the speaker stops answering.
    """

    _attr_translation_key = "log_amount"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SoundboksCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_log_amount"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.log_amount


class BatterySensor(SoundboksEntity, SensorEntity):
    """Mirrors a battery sensor measured outside the speaker, which doesn't report its own."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: SoundboksCoordinator, source: str) -> None:
        super().__init__(coordinator)
        self._source = source
        self._attr_unique_id = f"{coordinator.address}_battery"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(self.hass, self._source, self._async_source_changed)
        )

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # Independent of the speaker: the level matters most while it's off
        return self.native_value is not None

    @property
    def native_value(self) -> float | None:
        state = self.hass.states.get(self._source)
        if state is None:
            return None
        try:
            return float(state.state)
        except ValueError:
            return None
