#!/usr/bin/python3
"""Droid Deck — manage multiple ADB devices and mirror/record/share them with scrcpy.

A native GTK4/libadwaita utility:
  * Live device list (USB + Wi-Fi) with model, Android version, battery, state
  * One-click mirroring per device via scrcpy (multiple simultaneous sessions)
  * Mirror & record to MP4 (drop the window into OBS or screenshare to "share")
  * Wireless: switch a USB device to Wi-Fi ADB, connect by IP, pair (Android 11+)
  * Screenshots, reboot, adb server restart
"""

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

APP_ID = "dev.coldfire.DroidDeck"
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "droid-deck")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "audio": True,
    "turn_screen_off": False,
    "stay_awake": True,
    "show_touches": False,
    "always_on_top": False,
    "max_size": "Native",
    "bitrate": "8M",
    "tcpip_port": "5555",
    "last_address": "192.168.1.100:5555",
}
MAX_SIZES = ["Native", "1920", "1600", "1366", "1024", "800"]
BITRATES = ["4M", "8M", "16M", "32M"]

CSS = b"""
.dd-pill {
  padding: 3px 10px;
  border-radius: 999px;
  font-weight: 700;
}
.dd-pill.usb  { color: @accent_color;  background: alpha(@accent_color, 0.12); }
.dd-pill.wifi { color: @success_color; background: alpha(@success_color, 0.12); }
.dd-pill.warn { color: @warning_color; background: alpha(@warning_color, 0.15); }
.dd-pill.err  { color: @error_color;   background: alpha(@error_color, 0.12); }
.dd-pill.live { color: @success_color; background: alpha(@success_color, 0.12); }
.dd-pill.rec  { color: @error_color;   background: alpha(@error_color, 0.12); }
.dd-batt-low  { color: @error_color; font-weight: 700; }
"""


def find_tool(name, fallback_globs):
    path = shutil.which(name)
    if path:
        return path
    for pattern in fallback_globs:
        for match in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            if os.access(match, os.X_OK):
                return match
    return None


ADB = find_tool("adb", ["~/Android/Sdk/platform-tools/adb"])
SCRCPY = find_tool("scrcpy", ["~/opt/scrcpy*/scrcpy"])


# --------------------------------------------------------------------------- adb


class AdbError(Exception):
    pass


def run_adb(args, timeout=8):
    if not ADB:
        raise AdbError("adb not found")
    try:
        proc = subprocess.run(
            [ADB, *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise AdbError(f"adb {' '.join(args)} timed out")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def list_devices():
    _, out = run_adb(["devices", "-l"], timeout=6)
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        extra = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(
            {
                "serial": serial,
                "state": state,
                "model": extra.get("model", "").replace("_", " "),
                "transport": "wifi" if ":" in serial else "usb",
            }
        )
    return devices


def get_props(serial):
    _, out = run_adb(
        [
            "-s",
            serial,
            "shell",
            "getprop ro.product.marketname; getprop ro.product.model; "
            "getprop ro.build.version.release; getprop ro.product.manufacturer",
        ]
    )
    lines = [l.strip() for l in out.splitlines()]
    lines += [""] * (4 - len(lines))
    return {
        "name": lines[0] or lines[1] or serial,
        "android": lines[2],
        "manufacturer": lines[3],
    }


def get_battery(serial):
    _, out = run_adb(["-s", serial, "shell", "dumpsys battery"])
    match = re.search(r"level:\s*(\d+)", out)
    return int(match.group(1)) if match else None


def get_device_ip(serial):
    # Preferred: exactly the interface the user cares about for Wi-Fi ADB.
    _, out = run_adb(["-s", serial, "shell", "ip addr show wlan0"])
    match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    if match:
        return match.group(1)
    # Fallback: routing table, in case the interface isn't named wlan0.
    _, out = run_adb(["-s", serial, "shell", "ip route"])
    for line in out.splitlines():
        if " src " in line:
            return line.split(" src ")[1].split()[0]
    return None


# ---------------------------------------------------------------------- settings


def load_settings():
    try:
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
        return {**DEFAULT_SETTINGS, **data}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as fh:
        json.dump(settings, fh, indent=2)


# --------------------------------------------------------------------- device row


class DeviceRow(Adw.ActionRow):
    """One row per device: avatar, pills, battery, mirror/stop, actions menu."""

    def __init__(self, win, serial):
        super().__init__()
        self.win = win
        self.serial = serial
        self._menu_key = None
        self.set_use_markup(False)
        self.set_activatable(False)

        avatar = Adw.Avatar(size=40, show_initials=False)
        avatar.set_icon_name("phone-symbolic")
        self.add_prefix(avatar)

        self.batt_label = Gtk.Label()
        self.batt_label.add_css_class("numeric")
        self.batt_label.add_css_class("dim-label")

        self.live_pill = Gtk.Label()
        self.live_pill.add_css_class("dd-pill")
        self.live_pill.add_css_class("caption")
        self.live_pill.set_visible(False)

        self.state_pill = Gtk.Label()
        self.state_pill.add_css_class("dd-pill")
        self.state_pill.add_css_class("caption")

        self.mirror_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.mirror_btn.add_css_class("pill")
        self.mirror_btn.connect("clicked", self._on_mirror_clicked)

        self.menu_btn = Gtk.MenuButton(
            icon_name="view-more-symbolic", valign=Gtk.Align.CENTER
        )
        self.menu_btn.add_css_class("flat")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_valign(Gtk.Align.CENTER)
        for widget in (
            self.batt_label,
            self.live_pill,
            self.state_pill,
            self.mirror_btn,
            self.menu_btn,
        ):
            box.append(widget)
        self.add_suffix(box)

        self._mirroring = False

    def _on_mirror_clicked(self, _btn):
        if self._mirroring:
            self.win.stop_mirror(self.serial)
        else:
            self.win.start_mirror(self.serial)

    def _set_pill(self, label, text, variant):
        for cls in ("usb", "wifi", "warn", "err", "live", "rec"):
            label.remove_css_class(cls)
        label.set_text(text)
        label.add_css_class(variant)

    def update(self, info, props, battery, session):
        serial = self.serial
        state = info["state"]
        authorized = state == "device"

        name = (props or {}).get("name") or info.get("model") or serial
        self.set_title(name)

        subtitle_bits = [serial]
        if props and props.get("android"):
            subtitle_bits.append(f"Android {props['android']}")
        if props and props.get("manufacturer"):
            subtitle_bits.append(props["manufacturer"])
        self.set_subtitle("  ·  ".join(subtitle_bits))

        # battery
        if authorized and battery is not None:
            self.batt_label.set_text(f"{battery}%")
            self.batt_label.set_visible(True)
            if battery <= 20:
                self.batt_label.remove_css_class("dim-label")
                self.batt_label.add_css_class("dd-batt-low")
            else:
                self.batt_label.remove_css_class("dd-batt-low")
                self.batt_label.add_css_class("dim-label")
        else:
            self.batt_label.set_visible(False)

        # state pill
        if authorized:
            if info["transport"] == "wifi":
                self._set_pill(self.state_pill, "Wi-Fi", "wifi")
            else:
                self._set_pill(self.state_pill, "USB", "usb")
        elif state == "unauthorized":
            self._set_pill(self.state_pill, "Unauthorized — check phone", "warn")
        elif state == "offline":
            self._set_pill(self.state_pill, "Offline", "err")
        else:
            self._set_pill(self.state_pill, state.capitalize(), "warn")

        # live / rec pill
        self._mirroring = session is not None
        if session:
            if session.get("record_path"):
                self._set_pill(self.live_pill, "● REC", "rec")
            else:
                self._set_pill(self.live_pill, "● LIVE", "live")
            self.live_pill.set_visible(True)
        else:
            self.live_pill.set_visible(False)

        # mirror button
        self.mirror_btn.set_visible(authorized)
        self.mirror_btn.remove_css_class("suggested-action")
        self.mirror_btn.remove_css_class("destructive-action")
        if self._mirroring:
            self.mirror_btn.set_label("Stop")
            self.mirror_btn.add_css_class("destructive-action")
            self.mirror_btn.set_tooltip_text("Stop the scrcpy session")
        else:
            self.mirror_btn.set_label("Mirror")
            self.mirror_btn.add_css_class("suggested-action")
            self.mirror_btn.set_tooltip_text("Mirror this device with scrcpy")

        # actions menu (rebuild only when shape changes)
        menu_key = (authorized, info["transport"])
        if menu_key != self._menu_key:
            self._menu_key = menu_key
            self.menu_btn.set_menu_model(self._build_menu(authorized, info["transport"]))
        self.menu_btn.set_visible(authorized or info["transport"] == "wifi")

    def _build_menu(self, authorized, transport):
        q = self.serial
        menu = Gio.Menu()
        if authorized:
            section = Gio.Menu()
            section.append("Mirror & Record", f"win.record('{q}')")
            section.append("Screenshot", f"win.screenshot('{q}')")
            menu.append_section(None, section)
            section = Gio.Menu()
            if transport == "usb":
                section.append("Switch to Wi-Fi", f"win.to-wifi('{q}')")
            menu.append_section(None, section)
        if transport == "wifi":
            section = Gio.Menu()
            section.append("Disconnect", f"win.disconnect('{q}')")
            menu.append_section(None, section)
        if authorized:
            section = Gio.Menu()
            section.append("Reboot…", f"win.reboot('{q}')")
            menu.append_section(None, section)
        return menu


# ------------------------------------------------------------------------ window


class DroidDeckWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Droid Deck")
        self.set_default_size(600, 680)

        self.settings = load_settings()
        self.rows = {}          # serial -> DeviceRow
        self.sessions = {}      # serial -> {"proc": Popen, "record_path": str|None}
        self._props = {}        # serial -> props dict
        self._battery = {}      # serial -> (level, fetched_at)
        self._devices = []      # last device snapshot (for mirror-all etc.)
        self._stop = threading.Event()

        self._build_actions()
        self._build_ui()

        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.connect("close-request", self._on_close)

    # ---------------------------------------------------------------- UI build

    def _build_ui(self):
        self.toasts = Adw.ToastOverlay()

        header = Adw.HeaderBar()
        self.win_title = Adw.WindowTitle(title="Droid Deck", subtitle="No devices")
        header.set_title_widget(self.win_title)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh now (Ctrl+R)")
        refresh_btn.connect("clicked", lambda *_: self._refresh_async())
        header.pack_start(refresh_btn)

        wifi_btn = Gtk.Button(icon_name="network-wireless-symbolic")
        wifi_btn.set_tooltip_text("Connect a device over Wi-Fi")
        wifi_btn.connect("clicked", lambda *_: self.show_wireless_dialog())
        header.pack_end(self._menu_button())
        header.pack_end(wifi_btn)

        # device list page
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")

        list_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        heading = Gtk.Label(label="Devices", xalign=0)
        heading.add_css_class("heading")
        list_group.append(heading)
        list_group.append(self.listbox)

        clamp = Adw.Clamp(maximum_size=680, tightening_threshold=560)
        clamp.set_child(list_group)
        clamp.set_margin_top(18)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(clamp)

        # empty page
        self.empty_page = Adw.StatusPage(
            icon_name="phone-symbolic",
            title="No Devices Found",
            description=(
                "Plug in a device with USB debugging enabled,\n"
                "or connect one over Wi-Fi."
            ),
        )
        empty_btn = Gtk.Button(label="Connect over Wi-Fi", halign=Gtk.Align.CENTER)
        empty_btn.add_css_class("pill")
        empty_btn.add_css_class("suggested-action")
        empty_btn.connect("clicked", lambda *_: self.show_wireless_dialog())
        self.empty_page.set_child(empty_btn)

        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True
        )
        self.stack.add_named(self.empty_page, "empty")
        self.stack.add_named(scroller, "list")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        if not ADB or not SCRCPY:
            missing = " and ".join(
                name for name, path in (("adb", ADB), ("scrcpy", SCRCPY)) if not path
            )
            banner = Adw.Banner(title=f"{missing} not found on this system", revealed=True)
            content.append(banner)
        content.append(self.stack)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(content)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS.decode())
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _menu_button(self):
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Mirror All Devices", "win.mirror-all")
        section.append("Mirror Options…", "win.options")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Restart ADB Server", "win.restart-adb")
        section.append("About Droid Deck", "win.about")
        menu.append_section(None, section)
        btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        btn.set_tooltip_text("Main menu")
        return btn

    def _build_actions(self):
        simple = {
            "refresh": lambda *_: self._refresh_async(),
            "mirror-all": lambda *_: self.mirror_all(),
            "options": lambda *_: self.show_options_dialog(),
            "restart-adb": lambda *_: self.restart_adb(),
            "about": lambda *_: self.show_about(),
        }
        for name, cb in simple.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)

        param = {
            "record": lambda s: self.start_mirror(s, record=True),
            "screenshot": self.take_screenshot,
            "to-wifi": self.switch_to_wifi,
            "disconnect": self.disconnect_device,
            "reboot": self.confirm_reboot,
        }
        for name, cb in param.items():
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect(
                "activate", lambda _a, p, cb=cb: cb(p.get_string())
            )
            self.add_action(action)

    # ------------------------------------------------------------- poll & sync

    def _poll_loop(self):
        while not self._stop.is_set():
            self._refresh_once()
            self._stop.wait(2.5)

    def _refresh_async(self):
        threading.Thread(target=self._refresh_once, daemon=True).start()

    def _refresh_once(self):
        if not ADB:
            return
        try:
            devices = list_devices()
            for dev in devices:
                if dev["state"] != "device":
                    continue
                serial = dev["serial"]
                if serial not in self._props:
                    try:
                        self._props[serial] = get_props(serial)
                    except AdbError:
                        pass
                cached = self._battery.get(serial)
                if not cached or time.time() - cached[1] > 20:
                    try:
                        self._battery[serial] = (get_battery(serial), time.time())
                    except AdbError:
                        pass
            GLib.idle_add(self._apply_devices, devices)
        except AdbError:
            pass

    def _apply_devices(self, devices):
        self._devices = devices

        # reap finished scrcpy sessions
        for serial, session in list(self.sessions.items()):
            if session["proc"].poll() is not None:
                path = session.get("record_path")
                if path and os.path.exists(path):
                    self.toast(f"Recording saved: {os.path.basename(path)}", open_path=path)
                del self.sessions[serial]

        seen = set()
        for dev in devices:
            serial = dev["serial"]
            seen.add(serial)
            row = self.rows.get(serial)
            if row is None:
                row = DeviceRow(self, serial)
                self.rows[serial] = row
                self.listbox.append(row)
            battery_entry = self._battery.get(serial)
            row.update(
                dev,
                self._props.get(serial),
                battery_entry[0] if battery_entry else None,
                self.sessions.get(serial),
            )
        for serial in list(self.rows):
            if serial not in seen:
                self.listbox.remove(self.rows.pop(serial))

        count = len(devices)
        ready = sum(1 for d in devices if d["state"] == "device")
        live = len(self.sessions)
        if count == 0:
            subtitle = "No devices"
        else:
            subtitle = f"{ready} of {count} ready" if ready != count else (
                f"{count} device" + ("s" if count != 1 else "")
            )
            if live:
                subtitle += f"  ·  {live} mirroring"
        self.win_title.set_subtitle(subtitle)
        self.stack.set_visible_child_name("list" if count else "empty")
        return False

    # ---------------------------------------------------------------- mirroring

    def device_name(self, serial):
        props = self._props.get(serial)
        return (props or {}).get("name") or serial

    def start_mirror(self, serial, record=False):
        if not SCRCPY:
            self.toast("scrcpy not found")
            return
        if serial in self.sessions:
            self.toast(f"{self.device_name(serial)} is already mirroring")
            return

        name = self.device_name(serial)
        settings = self.settings
        cmd = [SCRCPY, "-s", serial, "--window-title", f"{name} — Droid Deck"]
        if not settings["audio"]:
            cmd.append("--no-audio")
        if settings["turn_screen_off"]:
            cmd.append("--turn-screen-off")
        if settings["stay_awake"]:
            cmd.append("--stay-awake")
        if settings["show_touches"]:
            cmd.append("--show-touches")
        if settings["always_on_top"]:
            cmd.append("--always-on-top")
        if settings["max_size"] != "Native":
            cmd += ["--max-size", settings["max_size"]]
        cmd += ["--video-bit-rate", settings["bitrate"]]

        record_path = None
        if record:
            videos = GLib.get_user_special_dir(
                GLib.UserDirectory.DIRECTORY_VIDEOS
            ) or os.path.expanduser("~/Videos")
            outdir = os.path.join(videos, "DroidDeck")
            os.makedirs(outdir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^\w.-]", "_", name)
            record_path = os.path.join(outdir, f"{safe}-{stamp}.mp4")
            cmd += ["--record", record_path]

        env = dict(os.environ)
        if ADB:
            env["ADB"] = ADB
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
        )
        self.sessions[serial] = {"proc": proc, "record_path": record_path}
        self.toast(("Recording " if record else "Mirroring ") + name)
        self._apply_devices(self._devices)

    def stop_mirror(self, serial):
        session = self.sessions.get(serial)
        if session:
            session["proc"].terminate()

    def mirror_all(self):
        started = 0
        for dev in self._devices:
            if dev["state"] == "device" and dev["serial"] not in self.sessions:
                self.start_mirror(dev["serial"])
                started += 1
        if not started:
            self.toast("Nothing to mirror")

    # ------------------------------------------------------------ device actions

    def take_screenshot(self, serial):
        def work():
            name = self.device_name(serial)
            try:
                proc = subprocess.run(
                    [ADB, "-s", serial, "exec-out", "screencap", "-p"],
                    capture_output=True,
                    timeout=15,
                )
                if proc.returncode != 0 or not proc.stdout:
                    raise AdbError("screencap failed")
                pictures = GLib.get_user_special_dir(
                    GLib.UserDirectory.DIRECTORY_PICTURES
                ) or os.path.expanduser("~/Pictures")
                outdir = os.path.join(pictures, "DroidDeck")
                os.makedirs(outdir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                safe = re.sub(r"[^\w.-]", "_", name)
                path = os.path.join(outdir, f"{safe}-{stamp}.png")
                with open(path, "wb") as fh:
                    fh.write(proc.stdout)
                GLib.idle_add(
                    self.toast, f"Screenshot saved: {os.path.basename(path)}", path
                )
            except (AdbError, subprocess.TimeoutExpired, OSError) as err:
                GLib.idle_add(self.toast, f"Screenshot failed: {err}")

        threading.Thread(target=work, daemon=True).start()

    def switch_to_wifi(self, serial):
        port = str(self.settings.get("tcpip_port", "5555")).strip() or "5555"

        def work():
            name = self.device_name(serial)
            try:
                ip = get_device_ip(serial)
                if not ip:
                    GLib.idle_add(
                        self.toast, f"{name}: no Wi-Fi address found (is Wi-Fi on?)"
                    )
                    return
                GLib.idle_add(self.toast, f"Restarting {name} adb on port {port}…")
                run_adb(["-s", serial, "tcpip", port])
                time.sleep(1.5)
                _, out = run_adb(["connect", f"{ip}:{port}"], timeout=15)
                GLib.idle_add(self.toast, out.strip() or f"Connected to {ip}:{port}")
            except AdbError as err:
                GLib.idle_add(self.toast, f"Wi-Fi switch failed: {err}")

        threading.Thread(target=work, daemon=True).start()

    def disconnect_device(self, serial):
        def work():
            try:
                _, out = run_adb(["disconnect", serial])
                GLib.idle_add(self.toast, out.strip() or f"Disconnected {serial}")
            except AdbError as err:
                GLib.idle_add(self.toast, str(err))

        threading.Thread(target=work, daemon=True).start()

    def confirm_reboot(self, serial):
        dialog = Adw.AlertDialog.new(
            "Reboot Device?", f"{self.device_name(serial)} will restart."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("reboot", "Reboot")
        dialog.set_response_appearance("reboot", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_dialog, response):
            if response == "reboot":
                threading.Thread(
                    target=lambda: run_adb(["-s", serial, "reboot"]), daemon=True
                ).start()
                self.toast(f"Rebooting {self.device_name(serial)}…")

        dialog.connect("response", on_response)
        dialog.present(self)

    def restart_adb(self):
        def work():
            try:
                run_adb(["kill-server"], timeout=10)
                run_adb(["start-server"], timeout=15)
                GLib.idle_add(self.toast, "ADB server restarted")
            except AdbError as err:
                GLib.idle_add(self.toast, f"ADB restart failed: {err}")

        self.toast("Restarting ADB server…")
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- dialogs

    def show_wireless_dialog(self):
        dialog = Adw.Dialog(title="Wireless Connection", content_width=440)

        connect_row = Adw.EntryRow(title="Device address (IP:port)")
        connect_row.set_text(self.settings.get("last_address", ""))

        connect_btn = Gtk.Button(label="Connect", halign=Gtk.Align.END)
        connect_btn.add_css_class("pill")
        connect_btn.add_css_class("suggested-action")

        connect_group = Adw.PreferencesGroup(
            title="Connect",
            description="Device must already have wireless debugging or ADB-over-TCP enabled.",
        )
        connect_group.add(connect_row)

        pair_addr_row = Adw.EntryRow(title="Pairing address (IP:port)")
        pair_code_row = Adw.EntryRow(title="Pairing code")
        pair_btn = Gtk.Button(label="Pair", halign=Gtk.Align.END)
        pair_btn.add_css_class("pill")

        pair_group = Adw.PreferencesGroup(
            title="Pair New Device",
            description="Android 11+: Settings → Developer options → Wireless debugging → Pair device with pairing code.",
        )
        pair_group.add(pair_addr_row)
        pair_group.add(pair_code_row)

        def do_connect(*_args):
            address = connect_row.get_text().strip()
            if not address:
                return
            if ":" not in address:
                address += ":5555"
            self.settings["last_address"] = address
            save_settings(self.settings)

            def work():
                try:
                    _, out = run_adb(["connect", address], timeout=15)
                    GLib.idle_add(self.toast, out.strip() or f"Connected to {address}")
                except AdbError as err:
                    GLib.idle_add(self.toast, str(err))

            threading.Thread(target=work, daemon=True).start()
            dialog.close()

        def do_pair(*_args):
            address = pair_addr_row.get_text().strip()
            code = pair_code_row.get_text().strip()
            if not address or not code:
                self.toast("Enter both pairing address and code")
                return

            def work():
                try:
                    _, out = run_adb(["pair", address, code], timeout=25)
                    GLib.idle_add(self.toast, out.strip() or "Paired")
                except AdbError as err:
                    GLib.idle_add(self.toast, str(err))

            threading.Thread(target=work, daemon=True).start()

        connect_btn.connect("clicked", do_connect)
        connect_row.connect("entry-activated", do_connect)
        pair_btn.connect("clicked", do_pair)
        pair_code_row.connect("entry-activated", do_pair)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for margin_setter in (
            body.set_margin_top,
            body.set_margin_bottom,
            body.set_margin_start,
            body.set_margin_end,
        ):
            margin_setter(18)
        body.append(connect_group)
        body.append(connect_btn)
        body.append(pair_group)
        body.append(pair_btn)

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(body)
        dialog.set_child(view)
        dialog.present(self)

    def show_options_dialog(self):
        dialog = Adw.PreferencesDialog(title="Mirror Options", content_width=440)
        page = Adw.PreferencesPage()

        display_group = Adw.PreferencesGroup(title="Display")
        size_row = Adw.ComboRow(
            title="Max resolution",
            subtitle="Limit the longer side of the video",
            model=Gtk.StringList.new(MAX_SIZES),
        )
        size_row.set_selected(
            MAX_SIZES.index(self.settings["max_size"])
            if self.settings["max_size"] in MAX_SIZES
            else 0
        )
        bitrate_row = Adw.ComboRow(
            title="Video bitrate", model=Gtk.StringList.new(BITRATES)
        )
        bitrate_row.set_selected(
            BITRATES.index(self.settings["bitrate"])
            if self.settings["bitrate"] in BITRATES
            else 1
        )
        top_row = Adw.SwitchRow(
            title="Always on top", active=self.settings["always_on_top"]
        )
        for row in (size_row, bitrate_row, top_row):
            display_group.add(row)

        behaviour_group = Adw.PreferencesGroup(title="Behaviour")
        audio_row = Adw.SwitchRow(
            title="Forward audio",
            subtitle="Requires Android 11+",
            active=self.settings["audio"],
        )
        screen_off_row = Adw.SwitchRow(
            title="Turn device screen off",
            subtitle="Mirror with the physical screen dark",
            active=self.settings["turn_screen_off"],
        )
        awake_row = Adw.SwitchRow(
            title="Keep device awake", active=self.settings["stay_awake"]
        )
        touches_row = Adw.SwitchRow(
            title="Show touches",
            subtitle="Handy for demos and recordings",
            active=self.settings["show_touches"],
        )
        for row in (audio_row, screen_off_row, awake_row, touches_row):
            behaviour_group.add(row)

        wireless_group = Adw.PreferencesGroup(
            title="Wireless",
            description="Port used by “Switch to Wi-Fi” when restarting adb over TCP/IP.",
        )
        try:
            port_value = float(self.settings.get("tcpip_port", "5555"))
        except (TypeError, ValueError):
            port_value = 5555.0
        port_row = Adw.SpinRow(
            title="ADB TCP/IP port",
            adjustment=Gtk.Adjustment(
                lower=1024,
                upper=65535,
                step_increment=1,
                page_increment=100,
                value=port_value,
            ),
        )
        wireless_group.add(port_row)

        page.add(display_group)
        page.add(behaviour_group)
        page.add(wireless_group)
        dialog.add(page)

        def bind_switch(row, key):
            row.connect(
                "notify::active",
                lambda r, _p: self._set_setting(key, r.get_active()),
            )

        bind_switch(top_row, "always_on_top")
        bind_switch(audio_row, "audio")
        bind_switch(screen_off_row, "turn_screen_off")
        bind_switch(awake_row, "stay_awake")
        bind_switch(touches_row, "show_touches")
        size_row.connect(
            "notify::selected",
            lambda r, _p: self._set_setting("max_size", MAX_SIZES[r.get_selected()]),
        )
        bitrate_row.connect(
            "notify::selected",
            lambda r, _p: self._set_setting("bitrate", BITRATES[r.get_selected()]),
        )
        port_row.connect(
            "notify::value",
            lambda r, _p: self._set_setting("tcpip_port", str(int(r.get_value()))),
        )

        dialog.present(self)

    def _set_setting(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)

    def show_about(self):
        about = Adw.AboutDialog(
            application_name="Droid Deck",
            application_icon="phone-symbolic",
            version="1.0",
            developer_name="coldfire",
            comments=(
                "Manage multiple ADB devices and mirror, record, or share "
                "them with scrcpy.\n\n"
                f"adb: {ADB or 'not found'}\nscrcpy: {SCRCPY or 'not found'}"
            ),
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self)

    # ------------------------------------------------------------------- misc

    def toast(self, message, open_path=None):
        toast = Adw.Toast.new(message)
        toast.set_timeout(4)
        if open_path:
            toast.set_button_label("Open")
            toast.connect(
                "button-clicked",
                lambda *_: Gtk.FileLauncher.new(
                    Gio.File.new_for_path(open_path)
                ).launch(self, None, None),
            )
        self.toasts.add_toast(toast)
        return False  # allow use directly in GLib.idle_add

    def _on_close(self, *_args):
        self._stop.set()
        return False


class DroidDeckApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.set_accels_for_action("win.refresh", ["<Ctrl>r"])
        self.set_accels_for_action("win.mirror-all", ["<Ctrl>m"])
        self.set_accels_for_action("window.close", ["<Ctrl>q"])

    def do_activate(self):
        window = self.props.active_window
        if not window:
            window = DroidDeckWindow(application=self)
            self.set_accels_for_action("win.options", ["<Ctrl>comma"])
        window.present()


if __name__ == "__main__":
    app = DroidDeckApp()
    raise SystemExit(app.run(None))
