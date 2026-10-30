"""Settling the active inputs, and what the advertisement tells without connecting."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soundboks.const import DOMAIN
from custom_components.soundboks.protocol import INPUTS_UUID, MANUFACTURER_ID, Input

from .conftest import ADDRESS, FakeSpeaker, advertise, setup_entry, setup_present, tick

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

SENSOR = "sensor.soundboks_202047_active_inputs"
GO_2_4_3 = {MANUFACTURER_ID: bytes.fromhex("00020403020102030405060708090a0b0c00")}
GO_3_0_0 = {MANUFACTURER_ID: bytes.fromhex("00030000020102030405060708090a0b0c00")}
# Sound status values as a Go sends them; the inputs are the second byte
AUX = bytes.fromhex("000400020008")
AUX_HOLD = bytes.fromhex("000c01020008")
BT_HOLD = bytes.fromhex("000801020008")
NOTHING = bytes.fromhex("000001020008")


async def open_link(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A command keeps the link open, which is when the speaker notifies."""
    await advertise(hass, entry)
    await entry.runtime_data.async_set_volume(50)


async def test_aux_unplug_flicker_is_settled(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
):
    fake_speaker.values[INPUTS_UUID] = AUX
    await setup_entry(hass, mock_entry)
    await open_link(hass, mock_entry)
    assert hass.states.get(SENSOR).state == "AUX"
    # AUX unplugged: the amplifier winds down through AUX + Bluetooth, then Bluetooth
    fake_speaker.notify(INPUTS_UUID, AUX_HOLD)
    await tick(hass, freezer, 1.5)
    fake_speaker.notify(INPUTS_UUID, BT_HOLD)
    await tick(hass, freezer, 1.5)
    fake_speaker.notify(INPUTS_UUID, NOTHING)
    assert hass.states.get(SENSOR).state == "AUX"
    # Repeats of the same status (it sends one a second) don't postpone it
    for _ in range(2):
        await tick(hass, freezer, 1)
        fake_speaker.notify(INPUTS_UUID, NOTHING)
    await tick(hass, freezer, 1.1)
    assert hass.states.get(SENSOR).state == "None"


async def test_flicker_back_to_shown_inputs_changes_nothing(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
):
    await setup_entry(hass, mock_entry)
    await open_link(hass, mock_entry)
    assert hass.states.get(SENSOR).state == "Bluetooth"
    fake_speaker.notify(INPUTS_UUID, NOTHING)
    await tick(hass, freezer, 1)
    assert hass.states.get(SENSOR).state == "Bluetooth"
    fake_speaker.notify(INPUTS_UUID, BT_HOLD)
    await tick(hass, freezer, 5)
    assert hass.states.get(SENSOR).state == "Bluetooth"


async def test_polled_inputs_settle_too(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    freezer: FrozenDateTimeFactory,
):
    coordinator = await setup_present(hass, mock_entry)
    fake_speaker.values[INPUTS_UUID] = AUX
    fake_speaker.values["7649b19f-c605-46e2-98f8-6c1808e0cfb4"] = b"\x10"
    await coordinator.async_refresh()
    # The rest of the poll shows at once, the inputs once settled
    assert coordinator.data.volume == 0x10
    assert coordinator.data.inputs is Input.BLUETOOTH
    await tick(hass, freezer, 3.1)
    assert coordinator.data.inputs is Input.AUX


async def test_advertised_model_and_firmware(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_present(hass, mock_entry, GO_2_4_3)
    coordinator = mock_entry.runtime_data
    # From the advertisement, not the firmware characteristic (the fake says 2.4.2)
    assert coordinator.firmware == "2.4.3"
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ADDRESS), mock_entry.entry_id
    )
    assert device.model == "SOUNDBOKS Go"
    assert device.sw_version == "2.4.3"


async def test_rpc_firmware_raises_issue_and_stops_connecting(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_present(hass, mock_entry, GO_3_0_0)
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"unsupported_firmware_{ADDRESS}") is not None
    assert fake_speaker.connect_count == 0
    with pytest.raises(HomeAssistantError) as raised:
        await mock_entry.runtime_data.async_set_volume(10)
    assert raised.value.translation_key == "unsupported_firmware"
    assert fake_speaker.connect_count == 0
    # e.g. downgraded again
    await advertise(hass, mock_entry, GO_2_4_3)
    assert issues.async_get_issue(DOMAIN, f"unsupported_firmware_{ADDRESS}") is None
    await mock_entry.runtime_data.async_set_volume(10)


async def test_removing_the_entry_removes_the_firmware_issue(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_present(hass, mock_entry, GO_3_0_0)
    await hass.config_entries.async_remove(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"unsupported_firmware_{ADDRESS}") is None


async def test_advert_without_a_registered_device(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    """The device may be gone from the registry (removed by hand): nothing to update then."""
    await setup_entry(hass, mock_entry)
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, ADDRESS), mock_entry.entry_id)
    registry.async_remove_device(device.id)
    await advertise(hass, mock_entry, GO_2_4_3)
    assert mock_entry.runtime_data.firmware == "2.4.3"
