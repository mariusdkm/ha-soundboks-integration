"""Constants for the SOUNDBOKS integration."""

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "soundboks"
CONF_PIN = "pin"
# Options: a script or button that turns the speaker on, and a battery sensor to show on its device
CONF_TURN_ON_ENTITY = "turn_on_entity"
CONF_BATTERY_ENTITY = "battery_entity"
# Option: the switch powering the speaker (a smart plug in its charger's line). Off then arms
# auto-boot and cuts the power; on restores it, and the speaker boots.
CONF_POWER_SWITCH = "power_switch"
MANUFACTURER = "SOUNDBOKS ApS"

POLL_INTERVAL = timedelta(seconds=30)
IDLE_TIMEOUT = 30.0  # seconds a command keeps the link open
# The speaker stops advertising while any app is connected, so "not advertising" may only mean
# busy: keep the last state this long before showing it off (see docs/PROTOCOL.md)
PRESENCE_GRACE = timedelta(minutes=5)
# An advertisement older than this doesn't count: it's a replay from before a restart, or the
# speaker is busy. Commands and polls only try to connect within it (or on an open link).
FRESH_ADVERTISEMENT = 15.0  # seconds
# After a command couldn't reach the speaker, further commands fail at once for this long instead
# of each trying to connect: a dragged slider sends a burst of them
COMMAND_BACKOFF = 10.0  # seconds
# A changed input only shows once it held this long: after AUX is unplugged the speaker reports
# AUX + Bluetooth, then Bluetooth, for a second or two each while the amplifier winds down
INPUT_SETTLE = 3.0  # seconds
# After HA powers the speaker off, adverts relayed while it shuts down are ignored this long
POWER_OFF_QUIET = timedelta(seconds=5)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]
