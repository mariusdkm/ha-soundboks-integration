"""Polls the speaker, tracks whether it is around, and runs commands."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta

from homeassistant.components.bluetooth import (
    MONOTONIC_TIME,
    BluetoothChange,
    BluetoothServiceInfoBleak,
    async_clear_advertisement_history,
    async_last_service_info,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ADDRESS,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
)
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import SoundboksClient
from .const import (
    COMMAND_BACKOFF,
    CONF_POLL_INTERVAL,
    CONF_POWER_SWITCH,
    DOMAIN,
    FRESH_ADVERTISEMENT,
    INPUT_SETTLE,
    POLL_INTERVAL,
    POWER_OFF_QUIET,
    PRESENCE_GRACE,
    RECONNECT_DELAY,
)
from .protocol import (
    Advertisement,
    Input,
    InvalidPinError,
    PinRequiredError,
    ProtocolError,
    RebootIncompleteError,
    SoundboksError,
    SoundProfile,
    SpeakerState,
    SpeakerUnreachableError,
    StereoRole,
    parse_advertisement,
)

_LOGGER = logging.getLogger(__name__)

type SoundboksConfigEntry = ConfigEntry[SoundboksCoordinator]


def firmware_issue_id(address: str) -> str:
    return f"unsupported_firmware_{address}"


class SoundboksCoordinator(DataUpdateCoordinator[SpeakerState | None]):
    """State is None until the speaker has been read once."""

    config_entry: SoundboksConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: SoundboksConfigEntry, client: SoundboksClient
    ) -> None:
        poll_interval = entry.options.get(CONF_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=POLL_INTERVAL
            if poll_interval is None
            else timedelta(seconds=float(poll_interval)),
        )
        self.client = client
        self.address: str = entry.data[CONF_ADDRESS]
        self.present = False
        self.firmware: str | None = None
        # The latest model, firmware and security the speaker advertised, if it sends them
        self.advertisement: Advertisement | None = None
        self._cancel_grace: CALLBACK_TYPE | None = None
        # Staying connected: the pending attempt to connect again after the link dropped
        self._cancel_reconnect: CALLBACK_TYPE | None = None
        self._cancel_quiet: CALLBACK_TYPE | None = None
        # The volume unmuting restores: the last non-zero one seen, None until then
        self._unmute_volume: int | None = None
        # Volume changes coalesce: only the latest pending one is written
        self._volume_lock = asyncio.Lock()
        # EQ band changes run in turn: each writes all six bands, built from the latest ones
        self._eq_lock = asyncio.Lock()
        self._volume_target: int | None = None
        # Commands fail fast until then, after one couldn't reach the speaker
        self._unreachable_until = 0.0
        # Inputs reported but not yet shown, waiting to hold for INPUT_SETTLE
        self._pending_inputs: Input | None = None
        self._cancel_settle: CALLBACK_TYPE | None = None

    @property
    def reachable(self) -> bool:
        """Worth trying to connect: the link is open, or the speaker advertised just now.

        A speaker that stopped advertising is off or held by another app; connecting would only
        time out.
        """
        return self.client.is_connected or self._advertising()

    def _advertising(self) -> bool:
        """The speaker advertised just now."""
        # HA's history, not our callback: callbacks only fire when an advertisement changes,
        # and the speaker repeats the same one
        last = async_last_service_info(self.hass, self.address, connectable=True)
        return last is not None and MONOTONIC_TIME() - last.time < FRESH_ADVERTISEMENT

    @property
    def supported(self) -> bool:
        """False once the speaker advertised firmware that no longer speaks this protocol."""
        return self.advertisement is None or self.advertisement.speaks_serial

    @property
    def power_switch(self) -> str | None:
        return self.config_entry.options.get(CONF_POWER_SWITCH)

    async def _connectable(self) -> bool:
        """Worth trying to connect: reachable, or a link nobody owned was just closed."""
        if not self.supported:
            return False
        if self.present and self.reachable:
            return True
        return await self.client.close_orphaned_link()

    async def _async_update_data(self) -> SpeakerState | None:
        if not await self._connectable():
            _LOGGER.debug("Speaker %s isn't advertising, skipping the poll", self.address)
            return self.data
        try:
            state = await self.client.read_state()
        except (InvalidPinError, PinRequiredError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SpeakerUnreachableError:
            # Most likely another app holds the speaker: keep what we know
            _LOGGER.debug("Speaker %s unreachable, keeping last state", self.address)
            self._link_gone()
            return self.data
        except ProtocolError as err:
            raise UpdateFailed(str(err)) from err
        if self.firmware is None:
            await self._async_read_firmware()
        self._unreachable_until = 0.0  # it answered: no need to hold commands back
        if self.client.keeps_link:
            self._cancel_grace_timer()  # it answered: it's here, advertising or not
        self._remember_volume(state.volume)
        return self._settle_inputs(state)

    async def _async_read_firmware(self) -> None:
        """Best effort: a failure here must not cost the state just read.

        Only for speakers that don't advertise their firmware.
        """
        try:
            self.firmware = await self.client.read_firmware()
        except SoundboksError:
            _LOGGER.debug("Could not read the firmware version, retrying at the next poll")
            return
        self._update_device_info()

    def _update_device_info(self) -> None:
        registry = dr.async_get(self.hass)
        if device := registry.async_get_device_by_identifier(
            (DOMAIN, self.address), self.config_entry.entry_id
        ):
            model = self.advertisement.model_name if self.advertisement else device.model
            registry.async_update_device(device.id, sw_version=self.firmware, model=model)

    @callback
    def _handle_advertised_info(self, advertisement: Advertisement | None) -> None:
        if advertisement is None or advertisement == self.advertisement:
            return
        self.advertisement = advertisement
        self.firmware = advertisement.firmware_version
        self._update_device_info()
        issue_id = firmware_issue_id(self.address)
        if advertisement.speaks_serial:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        _LOGGER.warning(
            "%s runs firmware %s, which this integration can't talk to; it stops connecting",
            self.address,
            self.firmware,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="unsupported_firmware",
            translation_placeholders={"name": self.config_entry.title, "firmware": self.firmware},
        )

    @callback
    def async_handle_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        self.client.set_ble_device(service_info.device)
        self._handle_advertised_info(parse_advertisement(service_info.manufacturer_data))
        if MONOTONIC_TIME() - service_info.time > FRESH_ADVERTISEMENT:
            return  # replayed from history: says nothing about now
        if self._cancel_quiet is not None:
            return  # relayed while the speaker we just turned off shuts down
        self._cancel_grace_timer()
        if not self.present:
            self.present = True
            self.async_update_listeners()
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), "soundboks refresh on advertisement"
            )

    @callback
    def async_handle_unavailable(self, service_info: BluetoothServiceInfoBleak) -> None:
        if self.client.is_connected:
            return  # it stopped advertising because we hold the link
        if not self.present or self._cancel_grace is not None:
            return
        if not PRESENCE_GRACE:
            self._mark_absent()
            return
        self._cancel_grace = async_call_later(self.hass, PRESENCE_GRACE, self._grace_expired)

    @callback
    def _grace_expired(self, _now: object) -> None:
        self._cancel_grace = None
        self._mark_absent()

    @callback
    def async_handle_link_lost(self) -> None:
        """The speaker dropped a link we keep: off, out of range, or a glitch.

        HA saw no advertisements while we held the link, and won't report the speaker gone
        again, so the grace starts here. Connecting again is tried until the grace runs out.
        """
        if not self.client.keeps_link:
            return
        self._link_gone()
        if self._cancel_reconnect is None:
            self._cancel_reconnect = async_call_later(self.hass, RECONNECT_DELAY, self._reconnect)

    @callback
    def _reconnect(self, _now: object) -> None:
        """Connect again once the speaker advertises; a check, no radio traffic, until then."""
        self._cancel_reconnect = None
        if self.client.is_connected or not self.present:
            return
        if not self._advertising():
            self._cancel_reconnect = async_call_later(self.hass, RECONNECT_DELAY, self._reconnect)
            return
        _LOGGER.debug("Speaker %s advertises again, reconnecting", self.address)
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), "soundboks reconnect"
        )

    @callback
    def _link_gone(self) -> None:
        """Staying connected, the link is gone: start the grace unless the speaker advertises."""
        if not self.client.keeps_link or self.client.is_connected:
            return
        if self.present and self._cancel_grace is None and not self._advertising():
            self._cancel_grace = async_call_later(self.hass, PRESENCE_GRACE, self._grace_expired)

    @callback
    def async_handle_volume(self, raw: int) -> None:
        """The speaker notified a volume change, e.g. from its knob."""
        if self.data is not None and self.data.volume != raw:
            self.async_set_updated_data(replace(self.data, volume=raw))
        self._remember_volume(raw)

    @callback
    def async_handle_inputs(self, inputs: Input) -> None:
        """The speaker notified its sound status (every second or so while a link is open)."""
        self._observe_inputs(inputs)

    def _settle_inputs(self, state: SpeakerState) -> SpeakerState:
        """The polled state, with the inputs already shown until the polled ones have settled."""
        if self.data is None:
            return state
        self._observe_inputs(state.inputs)
        return replace(state, inputs=self.data.inputs)

    @callback
    def _observe_inputs(self, inputs: Input) -> None:
        if self.data is None or inputs == self._pending_inputs:
            return  # nothing shown yet, or already waiting for these
        self._cancel_settle_timer()
        if inputs != self.data.inputs:
            self._pending_inputs = inputs
            self._cancel_settle = async_call_later(self.hass, INPUT_SETTLE, self._inputs_settled)

    @callback
    def _inputs_settled(self, _now: object) -> None:
        self._cancel_settle = None
        inputs, self._pending_inputs = self._pending_inputs, None
        if inputs is not None:
            self._update_state(inputs=inputs)

    def _cancel_settle_timer(self) -> None:
        self._pending_inputs = None
        if self._cancel_settle is not None:
            self._cancel_settle()
            self._cancel_settle = None

    async def async_set_volume(self, raw: int) -> None:
        """Set the volume; a newer call supersedes one still waiting, which then returns.

        A dragged slider sends a call per step. Writing each in turn would keep the speaker busy
        long after the slider stopped, so a waiting call only writes the latest volume asked for.
        """
        self._volume_target = raw
        async with self._volume_lock:
            target, self._volume_target = self._volume_target, None
            if target is None:
                return  # an earlier caller already wrote a newer volume
            await self._command(lambda: self.client.set_volume(target))
            self._update_state(volume=target)
            self._remember_volume(target)

    async def async_set_muted(self, muted: bool) -> None:
        """Mute by setting volume 0; unmute restores the last non-zero volume.

        The speaker has no mute of its own. Muted means volume 0 rather than a flag of ours, so
        the state stays true across restarts and knob turns.
        """
        if self.data is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="unreachable")
        if muted:
            if self.data.volume:
                await self.async_set_volume(0)
            return
        if self.data.volume:
            return
        if self._unmute_volume is None:
            # e.g. muted before a restart: guessing a volume could be far too loud
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="nothing_to_unmute")
        await self.async_set_volume(self._unmute_volume)

    def _remember_volume(self, raw: int) -> None:
        if raw:
            self._unmute_volume = raw

    async def async_set_profile(self, profile: SoundProfile) -> None:
        await self._command(lambda: self.client.set_profile(profile))
        self._update_state(profile=profile)

    async def async_set_eq_band(self, band: int, db: int) -> None:
        """Set one custom EQ band, keeping the others; this selects the CUSTOM profile.

        Like the SOUNDBOKS app: bands only sound in CUSTOM, so moving one switches to it.
        """
        async with self._eq_lock:
            if self.data is None:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="unreachable")
            bands = list(self.data.eq)
            bands[band] = db
            eq = tuple(bands)
            await self._command(lambda: self.client.set_custom_eq(eq))
            self._update_state(profile=SoundProfile.CUSTOM, eq=eq)

    async def async_set_stereo(self, role: StereoRole) -> None:
        await self._command(lambda: self.client.set_stereo(role))
        self._update_state(stereo=role)

    async def async_power_off(self, context: Context | None = None) -> None:
        if (switch := self.power_switch) is None:
            await self._command(self.client.power_off)
        else:
            await self._async_cut_power(switch, context)
        self._turned_off()

    async def _async_cut_power(self, switch: str, context: Context | None) -> None:
        """Arm auto-boot, then switch the power off: switching it on boots the speaker.

        Never cuts the power of a speaker that might be running without arming first: it would
        then stay off when the power returns, until someone turns its knob.
        """
        self._check_entity(switch)
        if self.present:
            try:
                await self._command(self.client.arm_auto_boot)
            except HomeAssistantError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="auto_boot_not_armed"
                ) from err
        else:
            _LOGGER.info(
                "%s is already off, so auto-boot can't be armed: switching its power on won't"
                " start it, its knob will",
                self.address,
            )
        await self._async_switch(switch, SERVICE_TURN_OFF, context)

    async def async_power_on(self, context: Context | None = None) -> None:
        """Switch the speaker's power on; it boots if Home Assistant cut the power."""
        switch = self.power_switch
        if switch is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_power_switch")
        self._check_entity(switch)
        state = self.hass.states.get(switch)
        if state is not None and state.state == STATE_ON and not self.present:
            # Nothing armed auto-boot: e.g. the knob turned it off
            _LOGGER.warning(
                "%s has power but is off, so it can't start by itself: turn its knob",
                self.address,
            )
        await self._async_switch(switch, SERVICE_TURN_ON, context)

    def _check_entity(self, entity_id: str) -> None:
        if self.hass.states.get(entity_id) is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="entity_missing",
                translation_placeholders={"entity_id": entity_id},
            )

    async def _async_switch(self, entity_id: str, service: str, context: Context | None) -> None:
        await self.hass.services.async_call(
            entity_id.partition(".")[0],
            service,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
            context=context,
        )

    async def async_query(self, opcodes: tuple[str, ...]) -> dict[str, bytes | str]:
        """Read-only serial commands, for diagnostics; see SoundboksClient.query."""
        if not await self._connectable():
            raise SpeakerUnreachableError("the speaker isn't advertising")
        return await self.client.query(opcodes)

    async def async_reboot(self) -> None:
        await self._command(self.client.reboot)
        self._turned_off()

    @callback
    def _turned_off(self) -> None:
        """HA turned the speaker off: absent now, and ready to see it come back.

        It comes back with the very advertisement HA already holds, which HA wouldn't pass on, so
        the history is cleared once the speaker has gone quiet.
        """
        self._mark_absent()
        self._cancel_quiet_timer()
        self._cancel_quiet = async_call_later(self.hass, POWER_OFF_QUIET, self._quiet_over)

    @callback
    def _quiet_over(self, _now: object) -> None:
        self._cancel_quiet = None
        async_clear_advertisement_history(self.hass, self.address)

    def _cancel_quiet_timer(self) -> None:
        if self._cancel_quiet is not None:
            self._cancel_quiet()
            self._cancel_quiet = None

    async def async_shutdown(self) -> None:
        self._cancel_reconnect_timer()
        self._cancel_settle_timer()
        self._cancel_quiet_timer()
        self._cancel_grace_timer()
        await super().async_shutdown()

    async def _command(self, action: Callable[[], Awaitable[None]]) -> None:
        if not self.supported:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unsupported_firmware",
                translation_placeholders={"firmware": self.firmware or "?"},
            )
        if MONOTONIC_TIME() < self._unreachable_until or not await self._connectable():
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="unreachable")
        try:
            await action()
            self._unreachable_until = 0.0
        except RebootIncompleteError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="reboot_incomplete"
            ) from err
        except SpeakerUnreachableError as err:
            self._unreachable_until = MONOTONIC_TIME() + COMMAND_BACKOFF
            self._link_gone()
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="unreachable"
            ) from err
        except (InvalidPinError, PinRequiredError) as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="invalid_pin"
            ) from err

    def _update_state(self, **changes: object) -> None:
        if self.data is not None:
            self.async_set_updated_data(replace(self.data, **changes))

    @callback
    def _mark_absent(self) -> None:
        self._cancel_grace_timer()
        self._cancel_reconnect_timer()
        self._cancel_settle_timer()
        if self.present:
            self.present = False
            self.async_update_listeners()

    def _cancel_grace_timer(self) -> None:
        if self._cancel_grace is not None:
            self._cancel_grace()
            self._cancel_grace = None

    def _cancel_reconnect_timer(self) -> None:
        if self._cancel_reconnect is not None:
            self._cancel_reconnect()
            self._cancel_reconnect = None
