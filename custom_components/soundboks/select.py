"""Sound profile and stereo role pickers."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SoundboksConfigEntry, SoundboksCoordinator
from .entity import SoundboksEntity
from .protocol import SoundProfile, SpeakerState, StereoRole

PROFILE_OPTIONS = {
    "power": SoundProfile.POWER,
    "bass_plus": SoundProfile.BASS_PLUS,
    "indoor": SoundProfile.INDOOR,
    "custom": SoundProfile.CUSTOM,  # the stored custom EQ, set with the EQ band entities
}
STEREO_OPTIONS = {"mono": StereoRole.MONO, "left": StereoRole.LEFT, "right": StereoRole.RIGHT}


@dataclass(frozen=True, kw_only=True)
class SoundboksSelectDescription(SelectEntityDescription):
    values: dict[str, IntEnum]
    current: Callable[[SpeakerState], IntEnum]
    apply: Callable[[SoundboksCoordinator, IntEnum], Awaitable[None]]


SELECTS = (
    SoundboksSelectDescription(
        key="sound_profile",
        translation_key="sound_profile",
        options=list(PROFILE_OPTIONS),
        values=PROFILE_OPTIONS,
        current=lambda state: state.profile,
        apply=lambda coordinator, value: coordinator.async_set_profile(SoundProfile(value)),
    ),
    SoundboksSelectDescription(
        key="stereo_role",
        translation_key="stereo_role",
        entity_category=EntityCategory.CONFIG,
        options=list(STEREO_OPTIONS),
        values=STEREO_OPTIONS,
        current=lambda state: state.stereo,
        apply=lambda coordinator, value: coordinator.async_set_stereo(StereoRole(value)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SoundboksConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(SoundboksSelect(entry.runtime_data, description) for description in SELECTS)


class SoundboksSelect(SoundboksEntity, SelectEntity):
    entity_description: SoundboksSelectDescription

    def __init__(
        self, coordinator: SoundboksCoordinator, description: SoundboksSelectDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        current = self.entity_description.current(self.coordinator.data)
        return next(
            (
                option
                for option, value in self.entity_description.values.items()
                if value == current
            ),
            None,
        )

    async def async_select_option(self, option: str) -> None:
        await self.entity_description.apply(
            self.coordinator, self.entity_description.values[option]
        )
