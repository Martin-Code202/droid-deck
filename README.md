# Droid Deck

A native GTK4 / libadwaita utility for managing multiple ADB devices and
mirroring, recording, or sharing them with [scrcpy](https://github.com/Genymobile/scrcpy).

It follows your GNOME light/dark theme and accent color automatically.

## Features

- **Live device list** — model, serial, Android version, manufacturer, and
  battery for every connected device, refreshed every couple of seconds. A
  status pill shows `USB` / `Wi-Fi` / `Unauthorized` / `Offline` at a glance.
- **Mirror with one click** — run as many simultaneous scrcpy sessions as you
  have devices. Each window is titled with the device name, so it's easy to
  pick in OBS or a screenshare.
- **Mirror & record** — capture straight to an MP4 in `~/Videos/DroidDeck/`.
- **Wireless** — switch a USB device to Wi-Fi ADB in one click, connect by
  IP, or pair with a code (Android 11+).
- **Screenshots** — saved to `~/Pictures/DroidDeck/`.
- **Per-device actions** — reboot, disconnect, restart the ADB server.
- **Mirror options** — audio forwarding, turn screen off, stay awake, show
  touches, always-on-top, max resolution, and bitrate. Persisted between runs.

## Requirements

- Python 3 with PyGObject (GTK 4 + libadwaita 1)
- [`adb`](https://developer.android.com/tools/adb) (Android platform-tools)
- [`scrcpy`](https://github.com/Genymobile/scrcpy) 2.0+

On Debian/Ubuntu:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 adb scrcpy
```

`adb` and `scrcpy` are located on `PATH`; if they aren't installed there, the
app also searches common locations (`~/Android/Sdk/platform-tools`, `~/opt/scrcpy*`).

## Usage

```sh
python3 droid_deck.py
```

Plug in a device with USB debugging enabled, or use the Wi-Fi button to
connect one wirelessly. Then hit **Mirror**.

### Keyboard shortcuts

| Shortcut   | Action              |
| ---------- | ------------------- |
| `Ctrl+R`   | Refresh devices     |
| `Ctrl+M`   | Mirror all devices  |
| `Ctrl+,`   | Mirror options      |
| `Ctrl+Q`   | Quit                |

Closing Droid Deck leaves any running scrcpy windows open on purpose, so an
active recording or shared screen doesn't die with the manager.

## License

MIT — see [LICENSE](LICENSE).
