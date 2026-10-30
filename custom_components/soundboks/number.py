"""The six custom EQ bands, lowest first."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SoundboksConfigEntry, SoundboksCoordinator
from .entity import SoundboksEntity
from .protocol import EQ_BANDS, EQ_MAX_DB, EQ_MIN_DB


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SoundboksConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(EqBand(entry.runtime_data, band) for band in range(EQ_BANDS))


class EqBand(SoundboksEntity, NumberEntity):
    """One band of the stored custom EQ; moving it selects the Custom sound profile."""

    _attr_translation_key = "eq_band"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = EQ_MIN_DB
    _attr_native_max_value = EQ_MAX_DB
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "dB"

    def __init__(self, coordinator: SoundboksCoordinator, band: int) -> None:
        super().__init__(coordinator)
        self._band = band
        self._attr_unique_id = f"{coordinator.address}_eq_band_{band + 1}"
        self._attr_translation_placeholders = {"band": str(band + 1)}

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        return data.eq[self._band] if data is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_eq_band(self._band, round(value))
