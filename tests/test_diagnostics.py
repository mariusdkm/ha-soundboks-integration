"""Diagnostics download."""

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soundboks.const import CONF_PIN
from custom_components.soundboks.diagnostics import async_get_config_entry_diagnostics

from .conftest import FakeSpeaker, make_entry, setup_entry, setup_present

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


async def test_diagnostics_redacts_pin(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    fake_speaker.set_pin("1234")
    entry = make_entry(pin="1234")
    await setup_present(hass, entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"][CONF_PIN] == "**REDACTED**"
    assert "1234" not in str(diagnostics)
    assert diagnostics["present"] is True
    assert diagnostics["firmware"] == "2.4.2"
    assert diagnostics["state"]["volume"] == 0x2D


async def test_diagnostics_asks_the_speaker(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    fake_speaker: FakeSpeaker,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("custom_components.soundboks.client.RESPONSE_TIMEOUT", 0.01)
    fake_speaker.serial_replies = {
        b"fwv": [b"\x02\x2e\x04\x2e\x03"],
        b"pmv": [b"sb75"],
        b"gst": [b"\x01\x00\x00\x00\x01\x00"],
        b"gdc": [b"\x01\x00\x00\x00\x02\x08\x01"],
        b"gla": [b"\x01\x00\x00\x00\x01\x60"],
        b"gcm": [b"\x02\x06"],
        b"gss": [b"\x01\x00\x00\x00\x07123", b"4abc"],
        b"gsl": [b"\x01\x00\x00\x00\x03abc"],
        b"gma": [b"\x01\x00\x00\x00\x08" + bytes.fromhex("010000108a600000")],
        # agv unanswered: the query stops there
    }
    await setup_present(hass, mock_entry)
    speaker = (await async_get_config_entry_diagnostics(hass, mock_entry))["speaker"]
    assert speaker["fwv"] == {"hex": "022e042e03", "version": "2.4.3"}
    assert speaker["pmv"]["text"] == "sb75"
    assert speaker["gdc"] == {"hex": "0801"}
    assert speaker["gcm"] == "refused: error 06"
    assert speaker["gss"]["text"] == "1234abc"
    assert speaker["gma"]["mac"] == "60:8A:10:00:00:01"
    assert speaker["agv"].startswith("no valid answer")
    assert "agt" not in speaker
    sent = {data for _uuid, data in fake_speaker.writes}
    # Never asked: the settings hold the Bluetooth password; the rest are secrets or IDs
    assert not sent & {b"gu2", b"gus", b"gsn", b"ski", b"gdi", b"flg"}
    assert not mock_entry.runtime_data.client.is_connected


async def test_diagnostics_with_speaker_off(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_speaker: FakeSpeaker
):
    await setup_entry(hass, mock_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_entry)
    assert diagnostics["speaker"].startswith("not read")
    assert fake_speaker.connect_count == 0
