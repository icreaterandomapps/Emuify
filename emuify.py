"""
Emuify - Emuify - Portable Android Emulator Manager
---------------------------------
Expected layout (next to this script):
    sdk/   jdk/   avd/

Highlights vs. the original:
  * UI never freezes (imports, AVD creation, deletes run in background threads)
  * Correct SDK layout on import (system-images/android-X/<tag>/<abi>)
  * AVD list read straight from disk (instant) instead of spawning emulator.exe
  * Running / stopped status, Stop button, per-AVD launch logs
  * Launch options (GPU, boot mode, audio, extra args) saved to a JSON file
  * Edit AVD (RAM, CPU cores, VM heap), wipe-data launch, install APK via adb
  * Fixes AVD .ini paths automatically if you move the portable folder
  * Safer ZIP extraction, progress bar, log tab, right-click menus, shortcuts
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
import urllib.error
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

# Crisp text on high-DPI Windows screens (must run before Tk()).
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ============================================================
# PATHS
# ============================================================

# When running normally, use the script directory.
# When frozen by PyInstaller --onefile, __file__ points into the temporary
# extraction directory, which is NOT where the portable SDK/JDK/AVD live.
# Always keep the portable emulator data beside the real EXE in one-file mode.
if getattr(sys, "frozen", False):
    ANDROID_ROOT = Path(sys.executable).resolve().parent
else:
    ANDROID_ROOT = Path(__file__).resolve().parent

SDK = ANDROID_ROOT / "sdk"
JDK_ROOT = ANDROID_ROOT / "jdk"
AVD_HOME = ANDROID_ROOT / "avd"
LOG_DIR = ANDROID_ROOT / "logs"
DOWNLOAD_DIR = ANDROID_ROOT / "downloads"
SYSTEM_IMAGES = SDK / "system-images"
SETTINGS_FILE = ANDROID_ROOT / "emuify_settings.json"

IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""
BAT = ".bat" if IS_WIN else ""
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

EMULATOR = SDK / "emulator" / f"emulator{EXE}"
ADB = SDK / "platform-tools" / f"adb{EXE}"
AVDMANAGER = SDK / "cmdline-tools" / "latest" / "bin" / f"avdmanager{BAT}"

# ============================================================
# COLORS (old Android / Material)
# ============================================================

BG = "#2B2B2B"
BG_DARK = "#1E1E1E"
TOOLBAR = "#496B2A"
TOOLBAR_DARK = "#344C1E"
ACCENT = "#7FAF3A"
CARD = "#383838"
FIELD = "#252525"
BTN_GREY = "#454545"
BTN_GREY_HOVER = "#5B5B5B"
TEXT = "#F2F2F2"
TEXT_SECONDARY = "#B5B5B5"
RUNNING_FG = "#B8D87A"

ANDROID_VERSIONS = {
    14: "4.0", 15: "4.0.3", 16: "4.1", 17: "4.2", 18: "4.3", 19: "4.4",
    21: "5.0", 22: "5.1", 23: "6.0", 24: "7.0", 25: "7.1", 26: "8.0",
    27: "8.1", 28: "9", 29: "10", 30: "11", 31: "12", 32: "12L",
    33: "13", 34: "14", 35: "15", 36: "16",
}

GPU_MODES = ["swiftshader", "swiftshader_indirect", "host",
             "angle_indirect", "auto"]
BOOT_MODES = {
    "Cold boot every time": "cold",
    "Quick boot (use snapshots)": "quick",
    "Cold boot, save snapshot on exit": "cold_save",
}

DEFAULT_SETTINGS = {
    "gpu": "swiftshader",
    "boot": "cold",
    "no_audio": False,
    "extra": "",
    "delete_zip": True,
}

# ============================================================
# JDK / ENVIRONMENT
# ============================================================


def find_jdk():
    if not JDK_ROOT.exists():
        return None
    if (JDK_ROOT / "bin" / f"java{EXE}").exists():
        return JDK_ROOT
    for folder in sorted(JDK_ROOT.iterdir(), reverse=True):
        if folder.is_dir() and (folder / "bin" / f"java{EXE}").exists():
            return folder
    return None


JAVA_HOME = find_jdk()

if JAVA_HOME:
    os.environ["JAVA_HOME"] = str(JAVA_HOME)

os.environ["ANDROID_SDK_ROOT"] = str(SDK)
os.environ["ANDROID_HOME"] = str(SDK)
os.environ["ANDROID_AVD_HOME"] = str(AVD_HOME)

_extra_paths = [
    JAVA_HOME / "bin" if JAVA_HOME else None,
    SDK / "emulator",
    SDK / "platform-tools",
    SDK / "cmdline-tools" / "latest" / "bin",
]
os.environ["PATH"] = os.pathsep.join(
    [str(p) for p in _extra_paths if p and p.exists()]
    + [os.environ.get("PATH", "")]
)

AVD_HOME.mkdir(parents=True, exist_ok=True)
SYSTEM_IMAGES.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# SETTINGS
# ============================================================


def load_settings():
    data = dict(DEFAULT_SETTINGS)
    try:
        loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data.update({k: loaded[k] for k in DEFAULT_SETTINGS if k in loaded})
    except Exception:
        pass
    return data


def save_settings():
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2),
                                 encoding="utf-8")
    except OSError as e:
        log(f"Could not save settings: {e}")


settings = load_settings()

# ============================================================
# THREAD-SAFE UI QUEUE + BACKGROUND WORK
# ============================================================

ui_queue: "queue.Queue" = queue.Queue()


def ui(fn):
    """Schedule fn to run on the Tk main thread."""
    ui_queue.put(fn)


def run_bg(work, done=None):
    """Run work() in a thread; call done(result, error) on the UI thread."""
    def target():
        try:
            result, error = work(), None
        except Exception as e:  # noqa: BLE001
            result, error = None, e
            log(traceback.format_exc())
        if done:
            ui(lambda: done(result, error))

    threading.Thread(target=target, daemon=True).start()


# ============================================================
# COMMAND RUNNER
# ============================================================


def run_command(command, input_text=None, timeout=180):
    log("$ " + " ".join(str(c) for c in command))
    try:
        result = subprocess.run(
            [str(c) for c in command],
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ANDROID_ROOT),
            env=os.environ,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        if output:
            log(output)
        return result.returncode, output
    except subprocess.TimeoutExpired:
        log("Command timed out.")
        return -1, "The command timed out."
    except Exception as e:  # noqa: BLE001
        log(str(e))
        return -1, str(e)


# ============================================================
# INI HELPERS
# ============================================================


def read_ini(path):
    data = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for line in text.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    return data


def write_ini_values(path, values):
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8",
                               errors="replace").splitlines()
    except OSError:
        lines = []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def norm_sysdir(value):
    return value.replace("\\", "/").strip().strip("/").lower()


def is_within(child, parent):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def api_label(api):
    if api.isdigit() and int(api) in ANDROID_VERSIONS:
        return f"Android {ANDROID_VERSIONS[int(api)]} (API {api})"
    return f"API {api}"


def digits(value):
    return re.sub(r"\D", "", value or "")


# ============================================================
# PACKAGE.XML / SYSTEM IMAGES
# ============================================================


def parse_package_xml(data: bytes):
    package, display = "", ""
    try:
        root_el = ET.fromstring(data)
        for el in root_el.iter():
            tag = el.tag.lower()
            if tag.endswith("localpackage") and not package:
                package = el.attrib.get("path", "")
            elif tag.endswith("display-name") and not display:
                display = (el.text or "").strip()
    except ET.ParseError:
        pass
    return {"package": package, "display": display}


ABI_ALIASES = {
    "x86": "x86", "i686": "x86", "x86_64": "x86_64", "x64": "x86_64",
    "x86-64": "x86_64", "amd64": "x86_64", "arm": "armeabi-v7a",
    "armv7": "armeabi-v7a", "armeabi-v7a": "armeabi-v7a",
    "armeabi": "armeabi", "arm64": "arm64-v8a", "arm64-v8a": "arm64-v8a",
    "aarch64": "arm64-v8a",
}

TAG_DISPLAY = {
    "default": "Default",
    "google_apis": "Google APIs",
    "google_apis_playstore": "Google Play",
    "android-tv": "Android TV",
    "android-wear": "Wear OS",
    "android-automotive": "Android Automotive",
}


def normalize_abi(value):
    value = (value or "").strip().lower()
    return ABI_ALIASES.get(value, value)


def parse_properties(data):
    props = {}
    if not data:
        return props
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            props[key.strip()] = value.strip()
    return props


def read_zip_package(zip_path):
    """
    Work out what a system-image ZIP contains. The image lives in a folder
    (x86, x86_64, x64, ...) that holds system.img, source.properties,
    build.prop, kernels... package.xml is optional: old images don't ship
    one, so API / ABI / tag are read from source.properties, then build.prop,
    then package.xml, then the folder name.
    """
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise RuntimeError("The selected file is not a valid ZIP.")

    with z:
        lookup = {}  # lower-case normalized name -> real name
        for real in z.namelist():
            norm = real.replace("\\", "/")
            if not norm.endswith("/"):
                lookup[norm.lower()] = real

        roots = sorted(
            {n.rsplit("/", 1)[0] + "/" if "/" in n else ""
             for n in lookup if n.split("/")[-1] == "system.img"},
            key=lambda p: (p.count("/"), p))
        if not roots:
            raise RuntimeError(
                "No system.img was found in the ZIP, so this doesn't look "
                "like an Android system image.")
        prefix = roots[0]

        def read(filename):
            real = lookup.get((prefix + filename).lower())
            return z.read(real) if real else None

        props = parse_properties(read("source.properties"))
        build = parse_properties(read("build.prop"))
        xml_raw = read("package.xml") or b""
        xml = parse_package_xml(xml_raw)
        xml_parts = xml["package"].split(";")
        if len(xml_parts) < 4 or xml_parts[0] != "system-images":
            xml_parts = ["", "", "", ""]

    folder = prefix.strip("/").split("/")[-1] if prefix else ""

    api = (digits(props.get("AndroidVersion.ApiLevel"))
           or digits(build.get("ro.build.version.sdk"))
           or digits(xml_parts[1]))
    abi = (normalize_abi(props.get("SystemImage.Abi"))
           or normalize_abi(build.get("ro.product.cpu.abi"))
           or normalize_abi(xml_parts[3])
           or normalize_abi(folder))
    tag = props.get("SystemImage.TagId") or xml_parts[2] or "default"

    if not api:
        raise RuntimeError("Could not determine the Android API level "
                           "(no source.properties / build.prop values).")
    if not abi:
        raise RuntimeError("Could not determine the ABI (x86, x86_64, ...).")
    if not all(re.fullmatch(r"[\w.\-]+", v) for v in (api, abi, tag)):
        raise RuntimeError("The image metadata contains unexpected "
                           f"characters: {api} / {tag} / {abi}")

    package = f"system-images;android-{api};{tag};{abi}"
    desc = props.get("Pkg.Desc", "").split(",")[0].strip()
    source = ("source.properties" if props.get("AndroidVersion.ApiLevel")
              else "build.prop" if build.get("ro.build.version.sdk")
              else "package.xml" if xml["package"] else "folder name")

    return {
        "package": package,
        "parts": package.split(";"),
        "api": api, "abi": abi, "tag": tag,
        "prefix": prefix,
        "folder": folder or "(ZIP root)",
        "source": source,
        "display": xml["display"] or desc,
        "revision": digits(props.get("Pkg.Revision", "").split(".")[0]) or "1",
        # keep the ZIP's package.xml only if it matches what we detected
        "xml_ok": (xml["package"] == package
                   and b"type-details" in xml_raw.lower()),
    }


def write_package_xml(folder, info):
    tag_display = TAG_DISPLAY.get(info["tag"],
                                  info["tag"].replace("_", " ").title())
    name = info["display"] or f"{tag_display} {info['abi']} System Image"
    text = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ns2:repository xmlns:ns2="http://schemas.android.com/repository/android/common/02" xmlns:ns3="http://schemas.android.com/sdk/android/repo/sys-img2/03" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<localPackage path="{info['package']}" obsolete="false">
<type-details xsi:type="ns3:sysImgDetailsType">
<api-level>{info['api']}</api-level>
<tag><id>{info['tag']}</id><display>{escape(tag_display)}</display></tag>
<abi>{info['abi']}</abi>
</type-details>
<revision><major>{info['revision']}</major></revision>
<display-name>{escape(name)}</display-name>
</localPackage>
</ns2:repository>
"""
    (Path(folder) / "package.xml").write_text(text, encoding="utf-8")


image_lock = threading.Lock()


def install_zip(zip_path, dest, info, progress_cb):
    with image_lock:
        _install_zip(zip_path, dest, info, progress_cb)


def _install_zip(zip_path, dest, info, progress_cb):
    prefix = info["prefix"]
    staging = SYSTEM_IMAGES / f".staging-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            members = [m for m in z.infolist()
                       if not m.filename.endswith("/")
                       and m.filename.replace("\\", "/").startswith(prefix)]
            for m in members:
                if not is_within(staging / m.filename, staging):
                    raise RuntimeError(f"Unsafe path in ZIP:\n{m.filename}")
            total = sum(m.file_size for m in members) or 1
            done, last = 0, -1
            for m in members:
                z.extract(m, staging)
                done += m.file_size
                pct = int(done * 100 / total)
                if pct != last:
                    last = pct
                    progress_cb(pct)

        image_root = staging / prefix.strip("/") if prefix else staging
        if not (image_root / "system.img").exists():
            raise RuntimeError("Extraction finished but system.img is "
                               "missing.")
        if not info["xml_ok"]:
            write_package_xml(image_root, info)
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(image_root), str(dest))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def scan_images():
    images, seen = [], set()
    patterns = ("*/package.xml", "*/*/package.xml", "*/*/*/package.xml")
    for pattern in patterns:
        for xml in SYSTEM_IMAGES.glob(pattern):
            folder = xml.parent
            rel = folder.relative_to(SYSTEM_IMAGES).parts
            if folder in seen or any(p.startswith(".") for p in rel):
                continue
            seen.add(folder)
            try:
                info = parse_package_xml(xml.read_bytes())
            except OSError:
                continue
            package = info["package"]
            parts = package.split(";") if package else []
            if len(parts) >= 4 and parts[0] == "system-images":
                api_raw, tag, abi = parts[1], parts[2], parts[3]
            else:  # legacy / hand-made layout
                api_raw = rel[0]
                tag = rel[1] if len(rel) >= 3 else "default"
                abi = rel[-1]
                package = ";".join(["system-images", api_raw, tag, abi])
            images.append({
                "api": api_raw.replace("android-", ""),
                "abi": abi,
                "tag": tag,
                "package": package,
                "path": folder,
            })
    return sorted(images, key=lambda i: (
        int(i["api"]) if i["api"].isdigit() else 9999, i["api"], i["abi"]))


# ============================================================
# AVDS
# ============================================================


def repair_avd_paths():
    """Keep name.ini 'path=' valid when the portable folder is moved."""
    for ini in AVD_HOME.glob("*.ini"):
        avd_dir = AVD_HOME / f"{ini.stem}.avd"
        if not avd_dir.is_dir():
            continue
        current = read_ini(ini).get("path", "")
        if os.path.normcase(current) != os.path.normcase(str(avd_dir)):
            try:
                write_ini_values(ini, {"path": str(avd_dir),
                                       "path.rel": f"avd/{avd_dir.name}"})
            except OSError:
                pass


def scan_avds():
    repair_avd_paths()
    avds = []
    for ini in sorted(AVD_HOME.glob("*.ini"), key=lambda p: p.name.lower()):
        name = ini.stem
        avd_dir = AVD_HOME / f"{name}.avd"
        if not avd_dir.is_dir():
            continue
        cfg = read_ini(avd_dir / "config.ini")
        sysdir = cfg.get("image.sysdir.1", "")
        m = re.search(r"android-([^/\\]+)", sysdir)
        api = m.group(1) if m else ""
        ram = digits(cfg.get("hw.ramSize", ""))
        avds.append({
            "name": name,
            "dir": avd_dir,
            "config": cfg,
            "sysdir": sysdir,
            "api": api,
            "android": api_label(api) if api else "Unknown",
            "tag": cfg.get("tag.id", "default"),
            "abi": cfg.get("abi.type", "?"),
            "ram": f"{ram} MB" if ram else "Default",
        })
    return avds


def find_avd_kernel(avd):
    sysdir = avd["sysdir"]
    if not sysdir:
        return None, None
    native = sysdir.replace("\\", os.sep).replace("/", os.sep)
    image_dir = SDK / native
    if not image_dir.exists():
        image_dir = Path(native)
    if not image_dir.exists():
        return None, None
    for kind, filename in (("ranchu", "kernel-ranchu"),
                           ("qemu", "kernel-qemu")):
        if (image_dir / filename).exists():
            return kind, image_dir / filename
    return None, None


# ============================================================
# MAIN WINDOW (created early so helpers can reference it)
# ============================================================

root = tk.Tk()
root.withdraw()

if not EMULATOR.exists():
    messagebox.showerror("Emuify",
                         f"{EMULATOR.name} was not found:\n\n{EMULATOR}")
    sys.exit(1)

if JAVA_HOME is None:
    messagebox.showerror("Emuify",
                         f"JDK was not found inside:\n\n{JDK_ROOT}")
    sys.exit(1)

SCALE = root.winfo_fpixels("1i") / 96


def S(n):
    return int(n * SCALE)


root.title("Emuify - Android Emulator")

# Emuify icon. In a one-file build the portable icon may be beside the EXE.
# If it was bundled with --add-data, also try PyInstaller's extraction folder.
EMUIFY_ICON = ANDROID_ROOT / "emuify.ico"
if not EMUIFY_ICON.exists() and getattr(sys, "frozen", False):
    bundled_icon = Path(getattr(sys, "_MEIPASS", "")) / "emuify.ico"
    if bundled_icon.exists():
        EMUIFY_ICON = bundled_icon
if EMUIFY_ICON.exists():
    try:
        root.iconbitmap(str(EMUIFY_ICON))
    except Exception:
        pass
root.geometry(f"{S(860)}x{S(600)}")
root.minsize(S(700), S(480))
root.configure(bg=BG)
root.deiconify()

avd_cache: dict = {}
image_cache: dict = {}
device_cache: list = []
procs: dict = {}
state = {"busy": False}

status_var = tk.StringVar(value="Ready")

# ============================================================
# LOG / STATUS / BUSY HELPERS
# ============================================================


def _append_log(text):
    log_text.configure(state="normal")
    log_text.insert("end", text.rstrip() + "\n")
    if int(log_text.index("end-1c").split(".")[0]) > 3000:
        log_text.delete("1.0", "500.0")
    log_text.see("end")
    log_text.configure(state="disabled")


def log(text):
    ui(lambda: _append_log(text))


def set_busy(message=None, pct=None):
    if message is None:
        state["busy"] = False
        progress.stop()
        progress.configure(mode="determinate", value=0)
        progress.pack_forget()
        return
    state["busy"] = True
    status_var.set(message)
    progress.pack(side="right", padx=10, pady=6, before=status_label)
    if pct is None:
        progress.configure(mode="indeterminate")
        progress.start(12)
    else:
        progress.stop()
        progress.configure(mode="determinate", value=pct)


def guard_busy():
    if state["busy"]:
        messagebox.showinfo("Please wait",
                            "Another operation is still running.")
        return True
    return False


def show_error(title, text, parent=None):
    text = text.strip() or "Unknown error."
    if len(text) > 1500:
        text = "...\n" + text[-1500:] + "\n\n(See the Log tab for details.)"
    messagebox.showerror(title, text, parent=parent or root)


def open_folder(path):
    try:
        if IS_WIN:
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:  # noqa: BLE001
        show_error("Open folder", str(e))


# ============================================================
# STYLES
# ============================================================

style = ttk.Style()
try:
    style.theme_use("clam")
except tk.TclError:
    pass

# Retro desktop styling: compact, square, and deliberately non-modern.
style.configure("Treeview", background="#303030", foreground=TEXT,
                fieldbackground="#303030", rowheight=S(25), borderwidth=1,
                relief="sunken", font=("Tahoma", 9))
style.map("Treeview", background=[("selected", TOOLBAR)],
          foreground=[("selected", TEXT)])
style.configure("Treeview.Heading", background="#4A4A4A", foreground=TEXT,
                relief="raised", borderwidth=1, font=("Tahoma", 8, "bold"),
                padding=(5, 3))
style.map("Treeview.Heading", background=[("active", "#5A5A5A")])
style.configure("TCombobox", fieldbackground=FIELD, background=BTN_GREY,
                foreground=TEXT, arrowcolor=TEXT, bordercolor="#666666",
                lightcolor="#666666", darkcolor="#171717", padding=2)
style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
          foreground=[("readonly", TEXT)])
style.configure("TNotebook", background=BG_DARK, borderwidth=1,
                tabmargins=(2, 2, 2, 0))
style.configure("TNotebook.Tab", background="#3A3A3A", foreground=TEXT_SECONDARY,
                padding=(12, 5), borderwidth=1,
                font=("Tahoma", 8, "bold"))
style.map("TNotebook.Tab", background=[("selected", "#505050")],
          foreground=[("selected", TEXT)])
style.configure("Horizontal.TProgressbar", background=ACCENT,
                troughcolor="#181818", borderwidth=1, relief="sunken")
style.configure("Vertical.TScrollbar", background=BTN_GREY, troughcolor=BG_DARK,
                arrowcolor=TEXT, bordercolor="#111111")

root.option_add("*TCombobox*Listbox.background", FIELD)
root.option_add("*TCombobox*Listbox.foreground", TEXT)
root.option_add("*TCombobox*Listbox.selectBackground", TOOLBAR)
root.option_add("*TCombobox*Listbox.selectForeground", TEXT)

# ============================================================
# WIDGET HELPERS
# ============================================================

BUTTON_KINDS = {
    "primary": (TOOLBAR, ACCENT),
    "dark": (TOOLBAR_DARK, TOOLBAR),
    "grey": (BTN_GREY, BTN_GREY_HOVER),
    "toolbar": (TOOLBAR, ACCENT),
}


def make_button(parent, text, command, kind="grey", font_size=8,
                padx=12, pady=5):
    bg, hover = BUTTON_KINDS[kind]
    # Old Windows/Android-era beveled button instead of a modern flat pill.
    b = tk.Button(parent, text=text, command=command, bg=bg, fg=TEXT,
                  activebackground=hover, activeforeground=TEXT,
                  relief="raised", bd=2, highlightthickness=0,
                  cursor="hand2", font=("Tahoma", font_size, "bold"),
                  padx=padx, pady=pady)
    b.bind("<Enter>", lambda e: b.configure(bg=hover))
    b.bind("<Leave>", lambda e: b.configure(bg=bg))
    b.bind("<ButtonPress-1>", lambda e: b.configure(relief="sunken"))
    b.bind("<ButtonRelease-1>", lambda e: b.configure(relief="raised"))
    return b


def make_dialog(title, width, height):
    d = tk.Toplevel(root)
    d.title(title)
    d.configure(bg=BG)
    d.resizable(False, False)
    d.transient(root)
    root.update_idletasks()
    w, h = S(width), S(height)
    x = root.winfo_x() + (root.winfo_width() - w) // 2
    y = root.winfo_y() + (root.winfo_height() - h) // 2
    d.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
    try:
        d.wait_visibility()
        d.grab_set()
    except tk.TclError:
        pass
    tk.Label(d, text=title, bg=BG, fg=TEXT,
             font=("Tahoma", 13, "bold")).pack(anchor="w", padx=24,
                                                 pady=(20, 6))
    return d


def dialog_label(parent, text):
    tk.Label(parent, text=text, bg=BG, fg=TEXT_SECONDARY,
             font=("Segoe UI", 9)).pack(anchor="w", padx=24, pady=(10, 0))


def dialog_entry(parent, var):
    e = tk.Entry(parent, textvariable=var, bg=FIELD, fg=TEXT,
                 insertbackground=TEXT, relief="flat",
                 font=("Segoe UI", 10))
    e.pack(fill="x", padx=24, pady=(4, 0), ipady=6)
    return e


def dialog_combo(parent, var, values, readonly=True):
    c = ttk.Combobox(parent, textvariable=var, values=values,
                     state="readonly" if readonly else "normal")
    c.pack(fill="x", padx=24, pady=(4, 0), ipady=3)
    return c


def dialog_check(parent, text, var):
    tk.Checkbutton(parent, text=text, variable=var, bg=BG, fg=TEXT,
                   selectcolor=FIELD, activebackground=BG,
                   activeforeground=TEXT, font=("Segoe UI", 9),
                   bd=0, highlightthickness=0).pack(anchor="w", padx=24,
                                                    pady=(12, 0))


def dialog_buttons(parent, ok_text, ok_cmd):
    bar = tk.Frame(parent, bg=BG)
    bar.pack(side="bottom", fill="x", padx=24, pady=18)
    make_button(bar, "CANCEL", parent.destroy, "grey",
                font_size=9).pack(side="right", padx=(8, 0))
    make_button(bar, ok_text, ok_cmd, "primary",
                font_size=9).pack(side="right")


# ============================================================
# SELECTION HELPERS
# ============================================================


def selected_avd():
    sel = avd_tree.selection()
    return avd_cache.get(sel[0]) if sel else None


def selected_image():
    sel = image_tree.selection()
    return image_cache.get(sel[0]) if sel else None


def require_avd(title="Virtual device"):
    avd = selected_avd()
    if avd is None:
        messagebox.showinfo(title, "Select a virtual device first.")
    return avd


# ============================================================
# RUNNING STATE
# ============================================================


def pid_alive(pid):
    """True if pid is a live emulator/qemu process (guards against PID reuse)."""
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            if code.value != 259:  # STILL_ACTIVE
                return False
            size = ctypes.c_ulong(520)
            buf = ctypes.create_unicode_buffer(520)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                   ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
                return "qemu" in name or "emulator" in name
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def lock_says_running(avd_dir):
    """
    Lock files are NOT proof of a running emulator (they can be left behind).
    Read the PID stored in them and check that process is really alive.
    A lock that is exclusively held by another process (unreadable) also
    counts as running.
    """
    try:
        locks = list(avd_dir.glob("*.lock"))
    except OSError:
        return False
    for lock in locks:
        try:
            files = [lock] if lock.is_file() else (
                [f for f in lock.iterdir() if f.is_file()]
                if lock.is_dir() else [])
        except OSError:
            continue
        for f in files:
            try:
                if f.stat().st_size > 64:
                    continue
                text = f.read_text(encoding="utf-8", errors="replace")
            except PermissionError:
                return True  # held open by a live process
            except OSError:
                continue
            match = re.search(r"\d+", text)
            if match and pid_alive(int(match.group())):
                return True
    return False


def is_running(avd):
    info = procs.get(avd["name"])
    if info and info["proc"].poll() is None:
        return True
    return lock_says_running(avd["dir"])


def update_running_state():
    for name, avd in avd_cache.items():
        if not avd_tree.exists(name):
            continue
        running = is_running(avd)
        avd_tree.set(name, "status", "● Running" if running else "Stopped")
        avd_tree.item(name, tags=("running",) if running else ())


def tail_file(path, lines=15):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.strip().splitlines()[-lines:])
    except OSError:
        return ""


def monitor():
    root.after(2000, monitor)
    for name, info in list(procs.items()):
        rc = info["proc"].poll()
        if rc is None:
            continue
        info["log"].close()
        del procs[name]
        log(f"{name} exited with code {rc}.")
        if rc != 0 and time.time() - info["started"] < 30:
            show_error(
                "Emulator closed unexpectedly",
                f"{name} exited right after starting (code {rc}).\n\n"
                f"{tail_file(info['logpath'])}\n\n"
                f"Full log:\n{info['logpath']}")
    update_running_state()


# ============================================================
# LAUNCH / STOP
# ============================================================


def build_launch_command(avd, wipe=False):
    cmd = [str(EMULATOR), "-avd", avd["name"]]
    boot = settings["boot"]
    if boot == "cold":
        cmd.append("-no-snapshot")
    elif boot == "cold_save":
        cmd.append("-no-snapshot-load")
    cmd += ["-gpu", settings["gpu"] or "swiftshader"]
    if settings["no_audio"]:
        cmd.append("-no-audio")
    if wipe:
        cmd.append("-wipe-data")
    kernel_type, kernel = find_avd_kernel(avd)
    if kernel_type == "qemu":  # old system image
        cmd += ["-kernel", str(kernel)]
    if settings["extra"].strip():
        cmd += shlex.split(settings["extra"], posix=False)
    return cmd, kernel_type


def launch_avd(avd, wipe=False):
    if is_running(avd):
        messagebox.showinfo("Launch", f"{avd['name']} is already running.")
        return
    if wipe and not messagebox.askyesno(
            "Wipe data",
            f"Launch '{avd['name']}' and erase all its user data?"):
        return

    cmd, kernel_type = build_launch_command(avd, wipe)
    logpath = LOG_DIR / f"{avd['name']}.log"
    try:
        log_file = open(logpath, "w", encoding="utf-8", errors="replace")
        log("$ " + " ".join(cmd))
        proc = subprocess.Popen(
            cmd, cwd=str(SDK / "emulator"), env=os.environ,
            stdin=subprocess.DEVNULL, stdout=log_file,
            stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    except Exception as e:  # noqa: BLE001
        show_error("Launch Error", f"Could not start {avd['name']}.\n\n{e}")
        status_var.set("Launch failed.")
        return

    procs[avd["name"]] = {"proc": proc, "log": log_file,
                          "started": time.time(), "logpath": logpath}
    suffix = " (legacy kernel)" if kernel_type == "qemu" else ""
    status_var.set(f"Starting {avd['name']}{suffix}...")
    update_running_state()


def find_serial(avd_name):
    """Return the adb serial (emulator-XXXX) of a running AVD, or None."""
    if not ADB.exists():
        return None
    code, out = run_command([ADB, "devices"], timeout=20)
    for line in out.splitlines():
        m = re.match(r"(emulator-\d+)\s+(device|offline)", line.strip())
        if not m:
            continue
        code, name_out = run_command(
            [ADB, "-s", m.group(1), "emu", "avd", "name"], timeout=10)
        lines = [l.strip() for l in name_out.splitlines() if l.strip()]
        if lines and lines[0] == avd_name:
            return m.group(1)
    return None


def stop_avd():
    avd = require_avd("Stop")
    if avd is None:
        return
    if not is_running(avd):
        messagebox.showinfo("Stop", f"{avd['name']} is not running.")
        return
    if guard_busy():
        return

    def work():
        serial = find_serial(avd["name"])
        if serial:
            run_command([ADB, "-s", serial, "emu", "kill"], timeout=20)
            return "adb"
        info = procs.get(avd["name"])
        if info:
            if IS_WIN:
                run_command(["taskkill", "/PID", str(info["proc"].pid),
                             "/T", "/F"], timeout=20)
            else:
                info["proc"].terminate()
            return "kill"
        return None

    def done(result, error):
        set_busy(None)
        if error or result is None:
            show_error("Stop", str(error) if error else
                       "Could not find the running emulator.")
            return
        status_var.set(f"Stopping {avd['name']}...")

    set_busy(f"Stopping {avd['name']}...")
    run_bg(work, done)


def install_apk():
    avd = require_avd("Install APK")
    if avd is None:
        return
    if not ADB.exists():
        show_error("Install APK", f"adb was not found:\n\n{ADB}")
        return
    if guard_busy():
        return
    apk = filedialog.askopenfilename(
        title="Install APK",
        filetypes=[("Android package", "*.apk"), ("All files", "*.*")])
    if not apk:
        return

    def work():
        serial = find_serial(avd["name"])
        if not serial:
            raise RuntimeError(
                f"{avd['name']} is not running (or hasn't finished booting).")
        code, out = run_command([ADB, "-s", serial, "install", "-r", "-t",
                                 apk], timeout=300)
        if code != 0 or "Success" not in out:
            raise RuntimeError(out)
        return Path(apk).name

    def done(result, error):
        set_busy(None)
        if error:
            show_error("Install APK", str(error))
            status_var.set("APK install failed.")
        else:
            status_var.set(f"Installed {result} on {avd['name']}")

    set_busy(f"Installing {Path(apk).name}...")
    run_bg(work, done)


# ============================================================
# IMPORT SYSTEM IMAGE
# ============================================================


def import_system_image():
    if guard_busy():
        return
    zip_path = filedialog.askopenfilename(
        title="Import Android System Image",
        filetypes=[("Android System Image", "*.zip"),
                   ("All files", "*.*")])
    if not zip_path:
        return

    def after_read(info, error):
        set_busy(None)
        if error:
            show_error("Import System Image", str(error))
            status_var.set("Import failed.")
            return

        dest = SYSTEM_IMAGES.joinpath(*info["parts"][1:])
        notes = ("" if info["xml_ok"] else
                 "\n\nA valid package.xml will be created for it.")
        if not messagebox.askyesno(
                "Import System Image",
                "Android System Image\n\n"
                f"{api_label(info['api'])}\n"
                f"ABI:   {info['abi']}\n"
                f"Tag:   {info['tag']}\n"
                f"ZIP folder:  {info['folder']}  "
                f"(detected from {info['source']})\n\n"
                f"Package:\n{info['package']}{notes}\n\n"
                "Import this image?"):
            status_var.set("Import cancelled.")
            return
        if dest.exists() and not messagebox.askyesno(
                "System Image Exists",
                f"This image already exists:\n\n{dest}\n\nReplace it?"):
            status_var.set("Import cancelled.")
            return

        def work():
            install_zip(zip_path, dest, info,
                        lambda pct: ui(lambda: set_busy(
                            f"Extracting system image... {pct}%", pct)))

        def finished(_, err):
            set_busy(None)
            if err:
                show_error("Import Error", str(err))
                status_var.set("Import failed.")
                return
            status_var.set(f"Imported {api_label(info['api'])}")
            refresh_all()
            messagebox.showinfo(
                "Import Complete",
                f"System image imported.\n\nInstalled at:\n{dest}")

        set_busy("Extracting system image...", 0)
        run_bg(work, finished)

    set_busy("Reading system image...")
    run_bg(lambda: read_zip_package(zip_path), after_read)


def delete_system_image():
    img = selected_image()
    if img is None:
        messagebox.showinfo("Delete System Image",
                            "Select a system image first.")
        return
    if guard_busy():
        return

    rel = norm_sysdir("system-images/"
                      + img["path"].relative_to(SYSTEM_IMAGES).as_posix())
    users = [a["name"] for a in avd_cache.values()
             if norm_sysdir(a["sysdir"]) == rel]

    warning = (f"Delete this system image?\n\n{img['package']}\n\n"
               f"Location:\n{img['path']}")
    if users:
        warning += ("\n\nWARNING: these AVDs use this image and will "
                    "stop working:\n\n" + "\n".join(users))
    if not messagebox.askyesno("Delete System Image", warning):
        return

    def done(_, err):
        set_busy(None)
        if err:
            show_error("Delete Error", str(err))
        else:
            status_var.set("System image deleted.")
        refresh_all()

    set_busy("Deleting system image...")
    run_bg(lambda: shutil.rmtree(img["path"]), done)


# ============================================================
# CREATE / EDIT / DELETE AVD
# ============================================================


def create_avd():
    images = list(image_cache.values())
    if not images:
        messagebox.showinfo("Create AVD",
                            "No system images are installed.\n\n"
                            "Click IMPORT IMAGE first.")
        return
    if not AVDMANAGER.exists():
        show_error("Create AVD", f"avdmanager was not found:\n\n{AVDMANAGER}")
        return

    d = make_dialog("Create Virtual Device", 500, 500)

    dialog_label(d, "AVD name")
    name_var = tk.StringVar()
    name_entry = dialog_entry(d, name_var)

    dialog_label(d, "System image")
    labels = [f"{api_label(i['api'])}  |  {i['abi']}  |  {i['tag']}"
              for i in images]
    image_var = tk.StringVar(value=labels[-1])
    image_combo = dialog_combo(d, image_var, labels)

    dialog_label(d, "Device profile")
    device_var = tk.StringVar(value="(default)")
    device_combo = dialog_combo(d, device_var,
                                ["(default)"] + device_cache)

    dialog_label(d, "RAM in MB (optional)")
    ram_var = tk.StringVar()
    dialog_entry(d, ram_var)

    keyboard_var = tk.BooleanVar(value=True)
    dialog_check(d, "Enable PC keyboard input", keyboard_var)

    if not device_cache:
        def load_devices():
            code, out = run_command([AVDMANAGER, "list", "device", "-c"],
                                    timeout=60)
            return [l.strip() for l in out.splitlines()
                    if l.strip() and " " not in l.strip()] if code == 0 else []

        def devices_loaded(result, error):
            if result:
                device_cache[:] = result
                if d.winfo_exists():
                    device_combo.configure(values=["(default)"] + result)

        run_bg(load_devices, devices_loaded)

    def perform_create():
        name = name_var.get().strip()
        if not name:
            messagebox.showwarning("Create AVD", "Enter an AVD name.",
                                   parent=d)
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            messagebox.showwarning(
                "Create AVD",
                "Use only letters, numbers, underscore, dot and hyphen.",
                parent=d)
            return
        ram = digits(ram_var.get())
        if ram_var.get().strip() and (not ram or int(ram) < 256):
            messagebox.showwarning("Create AVD",
                                   "RAM must be a number of at least 256 MB.",
                                   parent=d)
            return
        if name in avd_cache and not messagebox.askyesno(
                "Create AVD", f"'{name}' already exists. Overwrite it?",
                parent=d):
            return

        index = max(image_combo.current(), 0)
        package = images[index]["package"]
        device = device_var.get()
        keyboard = keyboard_var.get()
        d.destroy()

        def work():
            cmd = [AVDMANAGER, "create", "avd", "-n", name, "-k", package,
                   "-f"]
            if device and device != "(default)":
                cmd += ["-d", device]
            code, out = run_command(cmd, input_text="no\n")
            if code != 0:
                raise RuntimeError(out)
            cfg = AVD_HOME / f"{name}.avd" / "config.ini"
            values = {}
            if ram:
                values["hw.ramSize"] = ram
            if keyboard:
                values["hw.keyboard"] = "yes"
            if values and cfg.exists():
                write_ini_values(cfg, values)

        def done(_, err):
            set_busy(None)
            if err:
                show_error("Create AVD", str(err))
                status_var.set("AVD creation failed.")
                return
            status_var.set(f"Created {name}")
            refresh_all()
            if avd_tree.exists(name):
                avd_tree.selection_set(name)

        set_busy(f"Creating {name}...")
        run_bg(work, done)

    dialog_buttons(d, "CREATE", perform_create)
    name_entry.focus_set()
    d.bind("<Return>", lambda e: perform_create())


def edit_avd():
    avd = require_avd("Edit AVD")
    if avd is None:
        return
    if is_running(avd):
        messagebox.showinfo("Edit AVD",
                            "Stop the emulator before editing it.")
        return
    cfg = avd["config"]

    d = make_dialog(f"Edit {avd['name']}", 460, 380)

    dialog_label(d, "RAM (MB)")
    ram_var = tk.StringVar(value=digits(cfg.get("hw.ramSize", "")))
    dialog_entry(d, ram_var)

    dialog_label(d, "CPU cores")
    cores_var = tk.StringVar(value=digits(cfg.get("hw.cpu.ncore", "")))
    dialog_entry(d, cores_var)

    dialog_label(d, "VM heap (MB)")
    heap_var = tk.StringVar(value=digits(cfg.get("vm.heapSize", "")))
    dialog_entry(d, heap_var)

    keyboard_var = tk.BooleanVar(value=cfg.get("hw.keyboard", "") == "yes")
    dialog_check(d, "Enable PC keyboard input", keyboard_var)

    def save():
        values = {}
        for key, var, minimum in (("hw.ramSize", ram_var, 256),
                                  ("hw.cpu.ncore", cores_var, 1),
                                  ("vm.heapSize", heap_var, 16)):
            raw = var.get().strip()
            if not raw:
                continue
            num = digits(raw)
            if not num or int(num) < minimum:
                messagebox.showwarning(
                    "Edit AVD", f"Invalid value for {key} (minimum "
                                f"{minimum}).", parent=d)
                return
            values[key] = num
        values["hw.keyboard"] = "yes" if keyboard_var.get() else "no"
        try:
            write_ini_values(avd["dir"] / "config.ini", values)
        except OSError as e:
            show_error("Edit AVD", str(e), parent=d)
            return
        d.destroy()
        status_var.set(f"Updated {avd['name']}")
        refresh_avds()

    dialog_buttons(d, "SAVE", save)


def delete_avd():
    avd = require_avd("Delete AVD")
    if avd is None:
        return
    if is_running(avd):
        messagebox.showinfo("Delete AVD",
                            "Stop the emulator before deleting it.")
        return
    if not messagebox.askyesno(
            "Delete Virtual Device",
            f"Delete '{avd['name']}'?\n\n"
            "This deletes the AVD configuration and all its user data."):
        return
    if guard_busy():
        return

    def work():
        shutil.rmtree(avd["dir"], ignore_errors=False)
        ini = AVD_HOME / f"{avd['name']}.ini"
        if ini.exists():
            ini.unlink()

    def done(_, err):
        set_busy(None)
        if err:
            show_error("Delete AVD", str(err))
        else:
            status_var.set(f"Deleted {avd['name']}")
        refresh_avds()

    set_busy(f"Deleting {avd['name']}...")
    run_bg(work, done)


# ============================================================
# DOWNLOAD SYSTEM IMAGES (paste direct links -> download -> auto import)
# ============================================================

dl_items: dict = {}
dl_queue: "queue.Queue" = queue.Queue()
dl_lock = threading.Lock()
dl_state = {"running": False}


class DownloadCancelled(Exception):
    pass


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def filename_from_url(url):
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    parts = [p for p in path.split("/") if p]
    # keep the parent folder: google_apis/x86-23_r10.zip and
    # android/x86-23_r10.zip are different images with the same name
    name = "_".join(parts[-2:]) if parts else "system-image"
    name = re.sub(r"[^\w.\-]", "_", name)
    if not name.lower().endswith(".zip"):
        name += ".zip"
    return name


def download_file(url, dest, cancel, progress_cb):
    """Download with resume support. Partial data is kept in <name>.part."""
    part = dest.with_name(dest.name + ".part")
    existing = part.stat().st_size if part.exists() else 0
    base_headers = {"User-Agent": "Mozilla/5.0 (AndroidEmulatorManager)"}

    def open_url(offset):
        headers = dict(base_headers)
        if offset:
            headers["Range"] = f"bytes={offset}-"
        return urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=30)

    try:
        resp = open_url(existing)
    except urllib.error.HTTPError as e:
        if e.code == 416 and existing:  # stale/invalid partial file
            part.unlink()
            existing = 0
            resp = open_url(0)
        else:
            raise

    with resp:
        status = getattr(resp, "status", None) or resp.getcode()
        if existing and status != 206:  # server ignored the Range header
            existing = 0
        length = int(resp.headers.get("Content-Length") or 0)
        total = existing + length if length else 0
        done = existing
        with open(part, "ab" if existing else "wb") as f:
            while True:
                if cancel.is_set():
                    raise DownloadCancelled()
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                progress_cb(done, total, False)
    progress_cb(done, total, True)

    if total and done < total:
        raise RuntimeError("The connection closed early. "
                           "Press RETRY to resume the download.")
    if dest.exists():
        dest.unlink()
    part.replace(dest)


def dl_set(iid, **cols):
    def apply():
        if dl_tree.exists(iid):
            for key, value in cols.items():
                dl_tree.set(iid, key, value)
    ui(apply)


def dl_finish(iid, result, text):
    dl_items[iid]["state"] = result

    def apply():
        if dl_tree.exists(iid):
            dl_tree.set(iid, "status", text)
            dl_tree.set(iid, "speed", "")
            dl_tree.item(iid, tags=(result,))
    ui(apply)


def dl_process(iid):
    item = dl_items[iid]
    url, cancel = item["url"], item["cancel"]
    dest = DOWNLOAD_DIR / item["file"]
    try:
        if cancel.is_set():
            raise DownloadCancelled()
        item["state"] = "downloading"

        if dest.exists() and zipfile.is_zipfile(dest):
            dl_set(iid, status="Using existing download", progress="100%")
        else:
            tick = {"t": time.time(), "b": None}

            def cb(done, total, final):
                now = time.time()
                if tick["b"] is None:
                    tick["b"] = done
                if not final and now - tick["t"] < 0.4:
                    return
                speed = (done - tick["b"]) / max(now - tick["t"], 1e-6)
                tick["t"], tick["b"] = now, done
                pct = f"{done * 100 // total}%" if total else fmt_size(done)
                dl_set(iid, size=fmt_size(total) if total else "?",
                       progress=pct,
                       speed="" if final else f"{fmt_size(speed)}/s",
                       status="Downloading")
                if not state["busy"]:
                    ui(lambda: status_var.set(
                        f"Downloading {item['file']}  {pct}"))

            dl_set(iid, status="Downloading")
            download_file(url, dest, cancel, cb)

        info = read_zip_package(dest)
        target = SYSTEM_IMAGES.joinpath(*info["parts"][1:])
        dl_set(iid, status="Importing 0%", progress="100%", speed="")
        install_zip(dest, target, info,
                    lambda pct: dl_set(iid, status=f"Importing {pct}%"))

        if settings.get("delete_zip", True):
            try:
                dest.unlink()
            except OSError:
                pass
        dl_finish(iid, "done",
                  f"✔ Imported {api_label(info['api'])} · {info['abi']}"
                  f" · {info['tag']}")
        log(f"Imported {info['package']} from {url}")
        ui(refresh_all)
        ui(lambda: status_var.set(f"Imported {api_label(info['api'])}"))

    except DownloadCancelled:
        if iid not in dl_items:
            return
        if item.get("state") == "pausing":
            dl_finish(iid, "paused", "Paused (partial download kept)")
        elif item.get("state") == "deleting":
            return
        else:
            dl_finish(iid, "cancelled", "Cancelled (partial download kept)")
    except urllib.error.HTTPError as e:
        dl_finish(iid, "failed", f"✖ HTTP {e.code}: {e.reason}")
        log(f"{url} -> HTTP {e.code} {e.reason}")
    except Exception as e:  # noqa: BLE001
        log(traceback.format_exc())
        msg = str(e).replace("\n", " ")
        if dest.exists() and not zipfile.is_zipfile(dest):
            try:
                dest.unlink()  # not a ZIP (e.g. an HTML error page)
            except OSError:
                pass
        elif dest.exists():
            msg += "  (ZIP kept in downloads folder)"
        dl_finish(iid, "failed", f"✖ {msg}")


def dl_worker():
    while True:
        with dl_lock:
            try:
                iid = dl_queue.get_nowait()
            except queue.Empty:
                dl_state["running"] = False
                return
        if iid in dl_items:
            dl_process(iid)


def dl_ensure_worker():
    with dl_lock:
        if dl_state["running"]:
            return
        dl_state["running"] = True
    threading.Thread(target=dl_worker, daemon=True).start()


BUILTIN_SYSTEM_IMAGES = [
    ("21", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-21_r05.zip"),
    ("22", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-22_r06.zip"),
    ("23", "x86", "https://dl.google.com/android/repository/sys-img/android/x86-23_r10.zip"),
    ("23", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-23_r10.zip"),
    ("24", "x86", "https://dl.google.com/android/repository/sys-img/android/x86-24_r08.zip"),
    ("24", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-24_r08.zip"),
    ("25", "x86", "https://dl.google.com/android/repository/sys-img/android/x86-25_r01.zip"),
    ("25", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-25_r01.zip"),
    ("26", "x86", "https://dl.google.com/android/repository/sys-img/android/x86-26_r01.zip"),
    ("26", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-26_r01.zip"),
    ("27", "x86", "https://dl.google.com/android/repository/sys-img/android/x86-27_r01.zip"),
    ("27", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-27_r01.zip"),
    ("28", "x86", "https://dl.google.com/android/repository/sys-img/android/x86-28_r04.zip"),
    ("28", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-28_r04.zip"),
    ("29", "x86", "https://dl.google.com/android/repository/sys-img/android/x86-29_r08-windows.zip"),
    ("29", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-29_r08-windows.zip"),
    ("30", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-30_r11.zip"),
    ("31", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-31_r05.zip"),
    ("32", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-32_r02.zip"),
    ("33", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-33_r02.zip"),
    ("34", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-34_r04.zip"),
    ("35", "x86_64", "https://dl.google.com/android/repository/sys-img/android/x86_64-35_r02.zip"),
]


def dl_add(urls):
    added = 0
    active = {i["url"] for i in dl_items.values()
              if i["state"] in ("queued", "downloading")}
    for url in urls:
        if url in active:
            log(f"Already in the queue: {url}")
            continue
        active.add(url)
        iid = uuid.uuid4().hex[:10]
        dl_items[iid] = {"url": url, "file": filename_from_url(url),
                         "cancel": threading.Event(), "state": "queued"}
        dl_tree.insert("", "end", iid=iid, tags=("queued",), values=(
            dl_items[iid]["file"], "?", "—", "", "Queued"))
        dl_queue.put(iid)
        added += 1
    if added:
        dl_ensure_worker()
    return added


def dl_start():
    urls = [u for u in re.split(r"\s+", dl_text.get("1.0", "end").strip())
            if u]
    good = [u for u in urls if u.lower().startswith(("http://", "https://"))]
    if not good:
        messagebox.showinfo(
            "Download",
            "Paste at least one direct link to a system image ZIP, e.g.\n\n"
            "https://dl.google.com/android/repository/sys-img/android/"
            "x86-23_r10.zip")
        return
    if len(good) < len(urls):
        log(f"Ignored {len(urls) - len(good)} entries that aren't "
            "http(s) links.")
    dl_add(good)
    dl_text.delete("1.0", "end")


def dl_paste():
    try:
        text = root.clipboard_get()
    except tk.TclError:
        return
    dl_text.insert("end", text.strip() + "\n")


def dl_selected():
    sel = dl_tree.selection()
    return (sel[0], dl_items[sel[0]]) if sel and sel[0] in dl_items \
        else (None, None)


def dl_pause():
    iid, item = dl_selected()
    if item is None:
        messagebox.showinfo("Pause", "Select a download first.")
        return
    if item["state"] == "queued":
        item["state"] = "paused"
        dl_set(iid, status="Paused", speed="")
    elif item["state"] == "downloading":
        item["state"] = "pausing"
        item["cancel"].set()
        dl_set(iid, status="Pausing...", speed="")


def dl_resume():
    iid, item = dl_selected()
    if item is None:
        messagebox.showinfo("Resume", "Select a paused download first.")
        return
    if item["state"] not in ("paused", "cancelled", "failed"):
        return
    item["cancel"] = threading.Event()
    item["state"] = "queued"
    dl_set(iid, status="Queued", speed="")
    dl_tree.item(iid, tags=("queued",))
    dl_queue.put(iid)
    dl_ensure_worker()


def dl_delete():
    iid, item = dl_selected()
    if item is None:
        messagebox.showinfo("Delete", "Select a download first.")
        return
    if not messagebox.askyesno("Delete download", f"Delete '{item['file']}' and its partial download?"):
        return
    item["state"] = "deleting"
    item["cancel"].set()
    for path in (DOWNLOAD_DIR / item["file"],
                 DOWNLOAD_DIR / (item["file"] + ".part")):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
    dl_items.pop(iid, None)
    if dl_tree.exists(iid):
        dl_tree.delete(iid)


def dl_cancel():
    iid, item = dl_selected()
    if item is None:
        return
    if item["state"] in ("queued", "downloading"):
        item["state"] = "cancelled"
        item["cancel"].set()
        dl_set(iid, status="Cancelled", speed="")


def dl_retry():
    iid, item = dl_selected()
    if item is None:
        messagebox.showinfo("Retry", "Select a download first.")
        return
    if item["state"] not in ("failed", "cancelled"):
        return
    item["cancel"] = threading.Event()
    item["state"] = "queued"
    dl_set(iid, status="Queued", speed="")
    dl_tree.item(iid, tags=("queued",))
    dl_queue.put(iid)
    dl_ensure_worker()


def dl_clear():
    for iid in list(dl_items):
        if dl_items[iid]["state"] in ("done", "failed", "cancelled"):
            del dl_items[iid]
            if dl_tree.exists(iid):
                dl_tree.delete(iid)


# ============================================================
# GOOGLE IMAGE CATALOG (reads Google's official repository index)
# ============================================================

CATALOG_BASE = "https://dl.google.com/android/repository/sys-img/{dir}/"
CATALOG_INDEXES = ("sys-img2-4.xml", "sys-img2-3.xml", "sys-img2-2.xml",
                   "sys-img2-1.xml", "sys-img.xml")
CATALOG_SOURCES = {
    "AOSP (Android, no Google APIs)": "android",
    "Google APIs": "google_apis",
    "Google Play": "google_apis_playstore",
    "Android TV": "android-tv",
    "Wear OS": "android-wear",
    "Automotive": "android-automotive",
}
catalog_cache: dict = {}


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _find(el, name):
    if el is None:
        return None
    for child in el.iter():
        if child is not el and _local(child.tag) == name:
            return child
    return None


def _text(el, name):
    child = _find(el, name)
    return (child.text or "").strip() if child is not None else ""


def parse_sysimg_xml(data, base_url):
    """Parse a Google sys-img index (new 'sys-img2-N' or old 'sys-img' format)."""
    items = []
    for el in ET.fromstring(data).iter():
        if _local(el.tag) not in ("remotepackage", "system-image"):
            continue
        parts = el.attrib.get("path", "").split(";")
        if len(parts) >= 4 and parts[0] == "system-images":
            api = parts[1].replace("android-", "")
            tag, abi = parts[2], parts[3]
        else:
            api = _text(el, "api-level")
            abi = _text(el, "abi")
            tag = _text(el, "tag-id") or _text(el, "id") or "default"
        rev_el = _find(el, "revision")
        rev = digits(_text(rev_el, "major")
                     or (rev_el.text if rev_el is not None else "")) or "0"
        url = _text(el, "url")
        if not (api and abi and url):
            continue
        items.append({
            "api": api, "tag": tag, "abi": abi, "rev": int(rev),
            "url": urllib.parse.urljoin(base_url, url),
            "size": int(digits(_text(el, "size")) or 0),
            "display": _text(el, "display-name") or _text(el, "description"),
        })
    return items


def load_catalog(directory):
    base = CATALOG_BASE.format(dir=directory)
    found, errors = {}, []
    for name in CATALOG_INDEXES:
        try:
            req = urllib.request.Request(
                base + name,
                headers={"User-Agent": "Mozilla/5.0 (AndroidEmulatorManager)"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                errors.append(f"{name}: HTTP {e.code}")
            continue
        except Exception as e:  # noqa: BLE001  (no internet, DNS, timeout)
            raise RuntimeError(f"Could not reach Google's server: {e}")
        try:
            items = parse_sysimg_xml(data, base)
        except ET.ParseError:
            errors.append(f"{name}: invalid XML")
            continue
        for it in items:  # keep the newest revision of each api/tag/abi
            key = (it["api"], it["tag"], it["abi"])
            if key not in found or it["rev"] > found[key]["rev"]:
                found[key] = it
    if not found:
        raise RuntimeError("Google's list contained no images. "
                           + "; ".join(errors))
    return sorted(found.values(), key=lambda i: (
        int(i["api"]) if i["api"].isdigit() else 9999, i["api"], i["abi"]))


def open_catalog():
    d = make_dialog("Google System Images", 940, 640)

    top = tk.Frame(d, bg=BG)
    top.pack(fill="x", padx=24, pady=(0, 6))
    src_var = tk.StringVar(value=next(iter(CATALOG_SOURCES)))
    abi_var = tk.StringVar(value="All ABIs")
    info_var = tk.StringVar(value="")
    src_combo = ttk.Combobox(top, textvariable=src_var, state="readonly",
                             values=list(CATALOG_SOURCES), width=32)
    src_combo.pack(side="left")
    abi_combo = ttk.Combobox(top, textvariable=abi_var, state="readonly",
                             values=["All ABIs", "x86", "x86_64",
                                     "armeabi-v7a", "arm64-v8a"], width=14)
    abi_combo.pack(side="left", padx=(8, 0))
    tk.Label(top, textvariable=info_var, bg=BG, fg=TEXT_SECONDARY,
             font=("Segoe UI", 9)).pack(side="left", padx=12)

    bar = tk.Frame(d, bg=BG)
    bar.pack(side="bottom", fill="x", padx=24, pady=16)

    tree = make_tree(
        d, ("api", "android", "abi", "rev", "size", "file"),
        ("API", "Android", "ABI", "Rev", "Size", "File"),
        (50, 170, 110, 60, 80, 290),
        anchors={"api": "center", "abi": "center", "rev": "center",
                 "size": "center"})
    tree.configure(selectmode="extended")

    all_items, shown = [], []

    def render():
        abi = abi_var.get()
        tree.delete(*tree.get_children())
        shown[:] = [i for i in all_items
                    if abi == "All ABIs" or i["abi"] == abi]
        for idx, it in enumerate(shown):
            tree.insert("", "end", iid=str(idx), values=(
                it["api"], api_label(it["api"]), it["abi"], f"r{it['rev']}",
                fmt_size(it["size"]) if it["size"] else "?",
                Path(urllib.parse.urlparse(it["url"]).path).name))
        total = sum(i["size"] for i in shown)
        info_var.set(f"{len(shown)} images · {fmt_size(total)} total")

    def load(_event=None):
        name = src_var.get()
        directory = CATALOG_SOURCES[name]
        if directory in catalog_cache:
            all_items[:] = catalog_cache[directory]
            render()
            return
        info_var.set("Loading Google's list...")
        tree.delete(*tree.get_children())
        shown.clear()

        def done(result, error):
            if not d.winfo_exists() or src_var.get() != name:
                return
            if error:
                info_var.set("")
                show_error("Google System Images", str(error), parent=d)
                return
            catalog_cache[directory] = result
            all_items[:] = result
            render()

        run_bg(lambda: load_catalog(directory), done)

    def selected_items():
        return [shown[int(i)] for i in tree.selection()]

    def queue_items(items):
        if not items:
            return
        added = dl_add([i["url"] for i in items])
        d.destroy()
        notebook.select(download_tab)
        status_var.set(f"Queued {added} download{'s' if added != 1 else ''}")

    def add_selected():
        items = selected_items()
        if not items:
            messagebox.showinfo("Google System Images",
                                "Select one or more images first.", parent=d)
            return
        queue_items(items)

    def add_all():
        if not shown:
            return
        total = sum(i["size"] for i in shown)
        if messagebox.askyesno(
                "Add all",
                f"Download all {len(shown)} images shown "
                f"({fmt_size(total)})?", parent=d):
            queue_items(list(shown))

    def links_text():
        items = selected_items() or shown
        return "\n".join(i["url"] for i in items), len(items)

    def copy_links():
        text, n = links_text()
        if n:
            root.clipboard_clear()
            root.clipboard_append(text)
            info_var.set(f"Copied {n} links to the clipboard")

    def save_links():
        text, n = links_text()
        if not n:
            return
        path = filedialog.asksaveasfilename(
            parent=d, defaultextension=".txt",
            initialfile="android-system-image-links.txt",
            filetypes=[("Text file", "*.txt")])
        if path:
            Path(path).write_text(text + "\n", encoding="utf-8")
            info_var.set(f"Saved {n} links")

    make_button(bar, "CLOSE", d.destroy, "grey", font_size=9).pack(
        side="right")
    make_button(bar, "ADD SELECTED TO DOWNLOADS", add_selected, "primary",
                font_size=9).pack(side="right", padx=(0, 8))
    make_button(bar, "ADD ALL SHOWN", add_all, "dark", font_size=9).pack(
        side="right", padx=(0, 8))
    make_button(bar, "SAVE LINKS...", save_links, "grey", font_size=9).pack(
        side="left")
    make_button(bar, "COPY LINKS", copy_links, "grey", font_size=9).pack(
        side="left", padx=(8, 0))

    src_combo.bind("<<ComboboxSelected>>", load)
    abi_combo.bind("<<ComboboxSelected>>", lambda e: render())
    load()


# ============================================================
# SETTINGS DIALOG
# ============================================================


def open_settings():
    d = make_dialog("Launch Options", 520, 430)

    dialog_label(d, "Graphics (-gpu)")
    gpu_var = tk.StringVar(value=settings["gpu"])
    dialog_combo(d, gpu_var, GPU_MODES, readonly=False)

    dialog_label(d, "Boot mode")
    reverse = {v: k for k, v in BOOT_MODES.items()}
    boot_var = tk.StringVar(value=reverse.get(settings["boot"],
                                              "Cold boot every time"))
    dialog_combo(d, boot_var, list(BOOT_MODES))

    audio_var = tk.BooleanVar(value=settings["no_audio"])
    dialog_check(d, "Disable audio (-no-audio)", audio_var)

    dialog_label(d, "Extra emulator arguments")
    extra_var = tk.StringVar(value=settings["extra"])
    dialog_entry(d, extra_var)

    def save():
        settings.update(gpu=gpu_var.get().strip() or "swiftshader",
                        boot=BOOT_MODES.get(boot_var.get(), "cold"),
                        no_audio=audio_var.get(),
                        extra=extra_var.get().strip())
        save_settings()
        d.destroy()
        status_var.set("Launch options saved.")

    dialog_buttons(d, "SAVE", save)


# ============================================================
# REFRESH
# ============================================================


def refresh_avds():
    global avd_cache
    current = selected_avd()
    current_name = current["name"] if current else None

    avd_tree.delete(*avd_tree.get_children())
    avd_cache = {a["name"]: a for a in scan_avds()}
    for a in avd_cache.values():
        avd_tree.insert("", "end", iid=a["name"], values=(
            a["name"], a["android"], f"{a['tag']} / {a['abi']}",
            a["ram"], "Stopped"))
    update_running_state()
    if current_name in avd_cache:
        avd_tree.selection_set(current_name)

    if not state["busy"]:
        n = len(avd_cache)
        status_var.set(
            f"{n} virtual device{'s' if n != 1 else ''}" if n else
            "No virtual devices. Import a system image, then create an AVD.")


def refresh_images():
    global image_cache
    image_tree.delete(*image_tree.get_children())
    image_cache = {str(i["path"]): i for i in scan_images()}
    for iid, i in image_cache.items():
        image_tree.insert("", "end", iid=iid, values=(
            i["api"], i["abi"], i["tag"], i["package"]))


def refresh_all():
    refresh_images()
    refresh_avds()


# ============================================================
# TOOLBAR
# ============================================================

toolbar = tk.Frame(root, bg=TOOLBAR, height=S(46), bd=1, relief="raised")
toolbar.pack(fill="x")
toolbar.pack_propagate(False)

tk.Label(toolbar, text="Emuify", bg=TOOLBAR, fg=TEXT,
         font=("Tahoma", 13, "bold")).pack(side="left", padx=18)

# Window controls.  The fullscreen button stays visible even when the
# normal title bar is being used, so fullscreen can be entered/exited
# without relying on the OS window buttons.
fullscreen_state = {"on": False}

def toggle_fullscreen():
    fullscreen_state["on"] = not fullscreen_state["on"]
    root.attributes("-fullscreen", fullscreen_state["on"])
    fullscreen_btn.configure(text="▣" if fullscreen_state["on"] else "⛶")

def leave_fullscreen(event=None):
    if fullscreen_state["on"]:
        fullscreen_state["on"] = False
        root.attributes("-fullscreen", False)
        fullscreen_btn.configure(text="⛶")

root.bind("<Escape>", leave_fullscreen)

for symbol, cmd in (("⚙", open_settings), ("⟳", lambda: refresh_all())):
    b = tk.Button(toolbar, text=symbol, command=cmd, bg=TOOLBAR, fg=TEXT,
                  activebackground=ACCENT, activeforeground=TEXT,
                  relief="raised", bd=2, font=("Tahoma", 12, "bold"),
                  cursor="hand2", padx=8, pady=2)
    b.pack(side="right")

fullscreen_btn = tk.Button(toolbar, text="⛶", command=toggle_fullscreen,
                           bg=TOOLBAR, fg=TEXT, activebackground=ACCENT,
                           activeforeground=TEXT, relief="flat", bd=0,
                           font=("Segoe UI Symbol", 16), cursor="hand2",
                           padx=8, pady=2)
fullscreen_btn.pack(side="right")

# ============================================================
# STATUS BAR (packed before notebook so it keeps its space)
# ============================================================

status_bar = tk.Frame(root, bg=BG_DARK, height=S(30))
status_bar.pack(fill="x", side="bottom")
status_bar.pack_propagate(False)

status_label = tk.Label(status_bar, textvariable=status_var, bg=BG_DARK,
                        fg=TEXT_SECONDARY, font=("Tahoma", 8), anchor="w")
status_label.pack(fill="both", expand=True, padx=8, pady=2)

progress = ttk.Progressbar(status_bar, length=S(180), mode="determinate",
                           style="Horizontal.TProgressbar")

# ============================================================
# TABS
# ============================================================

notebook = ttk.Notebook(root)
notebook.pack(fill="both", expand=True, padx=4, pady=4)


def make_tree(parent, columns, headings, widths, anchors=None):
    frame = tk.Frame(parent, bg=BG)
    frame.pack(fill="both", expand=True, padx=8, pady=5)
    tree = ttk.Treeview(frame, columns=columns, show="headings",
                        selectmode="browse")
    for col, head, width in zip(columns, headings, widths):
        tree.heading(col, text=head)
        tree.column(col, width=S(width),
                    anchor=(anchors or {}).get(col, "w"))
    tree.pack(side="left", fill="both", expand=True)
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    sb.pack(side="right", fill="y")
    tree.configure(yscrollcommand=sb.set)
    return tree


def make_menu(items):
    menu = tk.Menu(root, tearoff=0, bg=CARD, fg=TEXT, bd=0,
                   activebackground=TOOLBAR, activeforeground=TEXT,
                   font=("Segoe UI", 9))
    for label, cmd in items:
        if label is None:
            menu.add_separator()
        else:
            menu.add_command(label=label, command=cmd)
    return menu


def bind_context_menu(tree, menu):
    def popup(event):
        row = tree.identify_row(event.y)
        if row:
            tree.selection_set(row)
            menu.tk_popup(event.x_root, event.y_root)
    tree.bind("<Button-3>", popup)


# ---------------- Devices tab ----------------

devices_tab = tk.Frame(notebook, bg=BG)
notebook.add(devices_tab, text="  Devices  ")

device_bar = tk.Frame(devices_tab, bg=BG, height=S(45))
device_bar.pack(fill="x", padx=8, pady=(4, 5))
device_bar.pack_propagate(False)

tk.Label(device_bar, text="Virtual Devices", bg=BG, fg=TEXT,
         font=("Tahoma", 11, "bold")).pack(side="left")

make_button(device_bar, "DELETE", delete_avd, "grey").pack(
    side="right", padx=(5, 0))
make_button(device_bar, "EDIT", edit_avd, "grey").pack(
    side="right", padx=(5, 0))
make_button(device_bar, "+ CREATE AVD", create_avd, "primary").pack(
    side="right", padx=(5, 0))
make_button(device_bar, "IMPORT IMAGE", import_system_image, "dark").pack(
    side="right")

avd_tree = make_tree(
    devices_tab,
    ("name", "android", "image", "ram", "status"),
    ("Virtual Device", "Android", "Image", "RAM", "Status"),
    (230, 190, 170, 80, 100))
avd_tree.tag_configure("running", foreground=RUNNING_FG)


def launch_selected(wipe=False):
    avd = require_avd("Launch")
    if avd:
        launch_avd(avd, wipe)


avd_tree.bind("<Double-1>", lambda e: launch_selected()
              if avd_tree.identify_row(e.y) else None)
avd_tree.bind("<Return>", lambda e: launch_selected())
avd_tree.bind("<Delete>", lambda e: delete_avd())

bind_context_menu(avd_tree, make_menu([
    ("Launch", launch_selected),
    ("Launch and wipe data", lambda: launch_selected(True)),
    ("Stop", stop_avd),
    (None, None),
    ("Install APK...", install_apk),
    ("Edit...", edit_avd),
    ("Open AVD folder", lambda: (selected_avd() and
                                 open_folder(selected_avd()["dir"]))),
    (None, None),
    ("Delete", delete_avd),
]))

launch_bar = tk.Frame(devices_tab, bg=BG, height=S(50))
launch_bar.pack(fill="x", padx=8, pady=(4, 8))
launch_bar.pack_propagate(False)

make_button(launch_bar, "▶  LAUNCH SELECTED", launch_selected, "primary",
            font_size=9, padx=18, pady=7).pack(side="right")
make_button(launch_bar, "■  STOP", stop_avd, "grey", font_size=9).pack(
    side="right", padx=(0, 8))
make_button(launch_bar, "INSTALL APK", install_apk, "grey",
            font_size=9).pack(side="right", padx=(0, 8))

# ---------------- System images tab ----------------

images_tab = tk.Frame(notebook, bg=BG)
notebook.add(images_tab, text="  System Images  ")

image_bar = tk.Frame(images_tab, bg=BG, height=S(45))
image_bar.pack(fill="x", padx=8, pady=(4, 5))
image_bar.pack_propagate(False)

tk.Label(image_bar, text="Installed System Images", bg=BG, fg=TEXT,
         font=("Tahoma", 11, "bold")).pack(side="left")
make_button(image_bar, "DELETE IMAGE", delete_system_image, "grey").pack(
    side="right", padx=(5, 0))
make_button(image_bar, "IMPORT ZIP", import_system_image, "primary").pack(
    side="right")

image_tree = make_tree(
    images_tab,
    ("api", "abi", "tag", "package"),
    ("API", "ABI", "Tag", "Package"),
    (80, 100, 130, 420),
    anchors={"api": "center", "abi": "center", "tag": "center"})
image_tree.bind("<Delete>", lambda e: delete_system_image())
bind_context_menu(image_tree, make_menu([
    ("Open folder", lambda: (selected_image() and
                             open_folder(selected_image()["path"]))),
    (None, None),
    ("Delete", delete_system_image),
]))

# ---------------- Download tab ----------------

download_tab = tk.Frame(notebook, bg=BG)
notebook.add(download_tab, text="  Download  ")

dl_bar = tk.Frame(download_tab, bg=BG, height=S(45))
dl_bar.pack(fill="x", padx=8, pady=(4, 0))
dl_bar.pack_propagate(False)
tk.Label(dl_bar, text="Android System Images", bg=BG, fg=TEXT,
         font=("Tahoma", 11, "bold")).pack(side="left")
make_button(dl_bar, "OPEN DOWNLOADS FOLDER",
            lambda: open_folder(DOWNLOAD_DIR), "grey").pack(side="right")

tk.Label(download_tab,
         text="Built-in image list — select images to download. No XML/catalog lookup is used.",
         bg=BG, fg=TEXT_SECONDARY, font=("Segoe UI", 9),
         anchor="w").pack(fill="x", padx=16, pady=(2, 4))

builtin_tree = make_tree(
    download_tab,
    ("api", "android", "abi", "file", "url"),
    ("API", "Android", "ABI", "File", "URL"),
    (55, 150, 85, 260, 430),
    anchors={"api": "center", "abi": "center"})
builtin_tree.configure(selectmode="extended")

for n, (api, abi, url) in enumerate(BUILTIN_SYSTEM_IMAGES):
    builtin_tree.insert("", "end", iid=f"img{n}", values=(
        api, api_label(api), abi, Path(urllib.parse.urlparse(url).path).name, url))


def download_builtin_selected():
    rows = builtin_tree.selection()
    if not rows:
        messagebox.showinfo("Download", "Select one or more system images first.")
        return
    dl_add([BUILTIN_SYSTEM_IMAGES[int(row[3:])][2] for row in rows])
    notebook.select(download_tab)


def download_builtin_all():
    dl_add([url for _, _, url in BUILTIN_SYSTEM_IMAGES])
    notebook.select(download_tab)

builtin_actions = tk.Frame(download_tab, bg=BG)
builtin_actions.pack(fill="x", padx=8, pady=(2, 5))
make_button(builtin_actions, "DOWNLOAD SELECTED", download_builtin_selected,
            "primary", font_size=9).pack(side="right")
make_button(builtin_actions, "DOWNLOAD ALL 22", download_builtin_all,
            "grey", font_size=9).pack(side="right", padx=(0, 8))

dl_tree = make_tree(
    download_tab,
    ("file", "size", "progress", "speed", "status"),
    ("File", "Size", "Progress", "Speed", "Status"),
    (250, 80, 80, 90, 330),
    anchors={"size": "center", "progress": "center", "speed": "center"})
dl_tree.tag_configure("done", foreground=RUNNING_FG)
dl_tree.tag_configure("failed", foreground="#EF9A9A")
dl_tree.tag_configure("cancelled", foreground=TEXT_SECONDARY)
dl_tree.tag_configure("paused", foreground=TEXT_SECONDARY)
dl_tree.tag_configure("queued", foreground=TEXT)

dl_actions = tk.Frame(download_tab, bg=BG, height=S(50))
dl_actions.pack(fill="x", padx=8, pady=(4, 8))
dl_actions.pack_propagate(False)
make_button(dl_actions, "DELETE", dl_delete, "grey", font_size=9).pack(side="right")
make_button(dl_actions, "RESUME", dl_resume, "grey", font_size=9).pack(side="right", padx=(0, 8))
make_button(dl_actions, "PAUSE", dl_pause, "grey", font_size=9).pack(side="right", padx=(0, 8))
make_button(dl_actions, "RETRY", dl_retry, "grey", font_size=9).pack(side="right", padx=(0, 8))

# ---------------- Log tab ----------------

log_tab = tk.Frame(notebook, bg=BG)
notebook.add(log_tab, text="  Log  ")

log_bar = tk.Frame(log_tab, bg=BG, height=S(45))
log_bar.pack(fill="x", padx=8, pady=(4, 5))
log_bar.pack_propagate(False)
tk.Label(log_bar, text="Command Log", bg=BG, fg=TEXT,
         font=("Tahoma", 11, "bold")).pack(side="left")
make_button(log_bar, "OPEN LOGS FOLDER", lambda: open_folder(LOG_DIR),
            "grey").pack(side="right", padx=(5, 0))
make_button(log_bar, "CLEAR",
            lambda: (log_text.configure(state="normal"),
                     log_text.delete("1.0", "end"),
                     log_text.configure(state="disabled")),
            "grey").pack(side="right")

log_frame = tk.Frame(log_tab, bg=BG)
log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
log_text = tk.Text(log_frame, bg="#1B1B1B", fg="#DDDDDD", bd=0,
                   relief="flat", wrap="word", state="disabled",
                   font=("Consolas", 9), insertbackground=TEXT)
log_scroll = ttk.Scrollbar(log_frame, orient="vertical",
                           command=log_text.yview)
log_text.configure(yscrollcommand=log_scroll.set)
log_scroll.pack(side="right", fill="y")
log_text.pack(side="left", fill="both", expand=True)

# ============================================================
# START
# ============================================================


def pump():
    try:
        while True:
            fn = ui_queue.get_nowait()
            try:
                fn()
            except Exception:  # noqa: BLE001
                _append_log(traceback.format_exc())
    except queue.Empty:
        pass
    root.after(50, pump)


root.bind("<F5>", lambda e: refresh_all())

pump()
monitor()
refresh_all()
root.mainloop()
