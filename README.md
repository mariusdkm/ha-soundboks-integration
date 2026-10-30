# SOUNDBOKS for Home Assistant

A [Home Assistant](https://www.home-assistant.io/) integration that controls a SOUNDBOKS speaker
over Bluetooth LE: turn it off, set the volume, switch sound profile, all from dashboards and
automations.

> **Not affiliated with SOUNDBOKS.** This is an independent project, not made, endorsed or
> supported by SOUNDBOKS ApS. "SOUNDBOKS" is their trademark, used here only to say which
> speakers this works with. Use it at your own risk.

## What you get

One device per speaker, with:

| Entity | What it does |
|---|---|
| Media player | On/off state, volume (set, up, down), **turn off** (and **turn on** when a power switch or turn-on action is set), and the active inputs as its source |
| Sound profile (select) | Power, Bass+ or Indoor. Shows "Custom" when a custom EQ was set from the app |
| Stereo role (select) | Mono, Left or Right |
| Active inputs (sensor) | What is playing right now, e.g. "AUX" or "Bluetooth". A change shows once it held for 3 seconds, which hides the speaker's in-between states while its amplifier winds down |
| Reboot (button) | Restarts the speaker |
| Battery (sensor) | Only when a battery sensor is set in [Options](#options): mirrors it |

The speaker **can't wake itself remotely**: its Bluetooth radio is off along with it. Turn it on
by hand, and Home Assistant shows it as on within seconds. If it runs from a charger behind a smart
plug, set that plug as the **power switch**, and Home Assistant turns it on and off for you (see
[Options](#options)). If you have something that can press its power button for you, set it as the
**turn-on action** instead.

## Supported speakers

Tested on one speaker: hardware `5506_DUAL_SPK`, firmware `4217000100` (app protocol `2.4.2`).
Other speakers of the same generation probably work too, but are untested: reports are welcome,
working or not.

Newer generations use a different Bluetooth layout and aren't supported yet, and neither is
firmware 3.0 or newer on this generation (it speaks a newer protocol). Setup tells you so instead
of half-working; a speaker updated to firmware 3.0 later gets a repair notice and is left alone.

## Requirements

- Home Assistant 2026.10 or newer.
- Bluetooth that reaches the speaker: the host's own adapter, or an
  [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) placed near it.

## Installation

### With HACS

1. In HACS, open the ⋮ menu → **Custom repositories**.
2. Add `https://github.com/Roxxas96/ha-soundboks-integration`, type **Integration**.
3. Install **SOUNDBOKS**, then restart Home Assistant.

### By hand

Copy `custom_components/soundboks` into your Home Assistant `config/custom_components/` folder,
then restart Home Assistant.

## Setup

1. **Close the SOUNDBOKS app** on every phone: the speaker accepts one connection at a time.
2. Turn the speaker on. Home Assistant should discover it: go to **Settings → Devices &
   services** and select **Add** on the SOUNDBOKS card. You can also add it by hand: **Add
   integration → SOUNDBOKS**.
3. If the speaker has a PIN (set in the SOUNDBOKS app), enter it.

To change the PIN later, open the integration's ⋮ menu → **Reconfigure**. If you change it from
the app, Home Assistant asks for the new one.

## Options

Open the integration's **Configure** to set optional helpers. They're blank by default, and
nothing changes until you set them.

- **Power switch**: a switch, typically a smart plug, between the speaker's charger and the wall,
  for a speaker that runs from its charger alone (no battery attached). Turning the speaker off
  then first tells it to start by itself when its power returns, and switches the power off;
  turning it on switches the power back on, and it boots. Away from Home Assistant the speaker
  behaves as usual. Two catches:
  - If it can't reach the speaker to arm the restart (the SOUNDBOKS app is connected, for
    example), turning off fails and leaves the power on, rather than cutting it in a way only the
    knob can undo.
  - If the power is switched off some other way, directly at the plug or with the speaker already
    off by its knob, nothing was armed: only the knob turns it on again.

  It takes over from the turn-on action.
- **Turn-on action**: any script or button entity that turns your speaker on, for example one that
  triggers a device pressing its power button. The media player's power button then runs it. The
  media player only shows "on" once the speaker is actually up and advertising, usually a few
  seconds later.
- **Battery sensor**: this speaker doesn't report its battery over Bluetooth. If you measure it
  another way, pick that sensor here and the speaker's device gets a **Battery** sensor that
  follows it. It keeps its value while the speaker is off.

## How the connection is shared

The speaker takes **a single Bluetooth connection at a time**. While Home Assistant is connected,
the SOUNDBOKS app can't connect, and the other way round. So Home Assistant never stays connected
for long:

- **Every 30 seconds** it connects, reads the state, and disconnects right away.
- **After a command** (volume, profile…) it stays connected for 30 seconds, so a series of volume
  changes doesn't reconnect each time. Knob turns show up instantly during that window, and at
  the next poll otherwise.
- **Whether the speaker is on** comes from its Bluetooth advertisements, without connecting.

The speaker stops advertising while any app is connected to it, so Home Assistant can't tell "off"
from "busy with the app". It keeps showing the last state for 5 minutes before marking the speaker
off, unless it turned it off itself.

If you open the SOUNDBOKS app while Home Assistant is connected, the app may need a second try.
Commands sent from Home Assistant while the app is connected fail with a clear error.

## Limitations

- No power on over Bluetooth (see above).
- No XLR inputs, TeamUp, knob volume limits or power-on volume yet.
- No Siri volume control through HomeKit Bridge yet: HomeKit has no speaker volume control for
  media players.

## Troubleshooting

**The speaker isn't discovered, or says it can't connect.** Check that it's on, that no phone has
the SOUNDBOKS app open, and that it's in range. An ESPHome Bluetooth proxy near the speaker fixes
range problems.

**Debug logs.** Enable debug logging from the integration's ⋮ menu, or add this to
`configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.soundboks: debug
```

When reporting a bug, please include the logs and the diagnostics file (device page → ⋮ →
**Download diagnostics**; the PIN is removed from it). If the speaker is on, the download also asks
it a few read-only questions: firmware versions, self-test flags, its update manifest. Its settings
are left out, as they hold the Bluetooth password.

## How it works

[docs/PROTOCOL.md](docs/PROTOCOL.md) describes what the integration sends to the speaker. The
protocol was worked out for interoperability; no code or assets from the official app are included.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE).
