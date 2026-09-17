from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NAME = "codex-native-scheduler"
LABEL = "com.codex.native-scheduler"
VERSION = "1.4.6"
SRC = Path(__file__).resolve().parent
HOME = Path.home()
DEST = HOME / ".codex/plugins" / NAME
MARKET = HOME / ".agents/plugins/marketplace.json"
APP_HOME = HOME / "Library/Application Support/CodexNativeScheduler"
PLIST = HOME / "Library/LaunchAgents" / (LABEL + ".plist")
LOCAL_APP = HOME / "Applications/Codex Scheduler.app"


def shlex_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


def detect_codex():
    for p in [
        shutil.which("codex"),
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        str(HOME / "Applications/ChatGPT.app/Contents/Resources/codex"),
    ]:
        if p and Path(p).exists():
            return str(Path(p).resolve())
    return None


def copy_plugin():
    DEST.parent.mkdir(parents=True, exist_ok=True)
    temp = DEST.with_name(DEST.name + ".installing")
    shutil.rmtree(temp, ignore_errors=True)
    shutil.copytree(
        SRC,
        temp,
        ignore=shutil.ignore_patterns(
            ".git", ".github", ".gitignore", ".gitattributes",
            "__pycache__", "*.pyc", ".pytest_cache", ".coverage",
            "tests", "SECURITY.md", "CONTRIBUTING.md",
            "Install Codex Scheduler.command", "Uninstall Codex Scheduler.command",
            "*.zip",
        ),
    )
    shutil.rmtree(DEST, ignore_errors=True)
    temp.replace(DEST)


def write_mcp(py: str, codex: str | None):
    env = {"CODEX_NATIVE_SCHEDULER_HOME": str(APP_HOME)}
    if codex:
        env["CODEX_BIN"] = codex
    data = {NAME: {"command": py, "args": [str(DEST / "server/mcp_server.py")], "env": env}}
    (DEST / ".mcp.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def merge_marketplace():
    MARKET.parent.mkdir(parents=True, exist_ok=True)
    data = None
    if MARKET.exists():
        try:
            data = json.loads(MARKET.read_text(encoding="utf-8"))
        except Exception:
            bak = MARKET.with_suffix(".json.backup")
            shutil.copy2(MARKET, bak)
            raise RuntimeError(f"Existing marketplace.json is invalid. Backed it up to {bak}")
    if not isinstance(data, dict):
        data = {"name": "personal-local-plugins", "interface": {"displayName": "Personal Local Plugins"}, "plugins": []}
    if not isinstance(data.get("plugins"), list):
        data["plugins"] = []
    entry = {
        "name": NAME,
        "source": {"source": "local", "path": "./.codex/plugins/codex-native-scheduler"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }
    data["plugins"] = [p for p in data["plugins"] if not (isinstance(p, dict) and p.get("name") == NAME)] + [entry]
    MARKET.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def merge_settings(py: str, codex: str | None):
    APP_HOME.mkdir(parents=True, exist_ok=True)
    path = APP_HOME / "settings.json"
    settings = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            settings = {}
    if not isinstance(settings, dict):
        settings = {}
    settings.update({"version": 2, "python_bin": py})
    if codex:
        settings["codex_bin"] = codex
    settings.setdefault("notifications", True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def write_service(py: str, codex: str | None):
    APP_HOME.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    merge_settings(py, codex)
    env = {"CODEX_NATIVE_SCHEDULER_HOME": str(APP_HOME)}
    if codex:
        env["CODEX_BIN"] = codex
    pl = {
        "Label": LABEL,
        "ProgramArguments": [py, str(DEST / "server/worker.py")],
        "RunAtLoad": True,
        "StartInterval": 30,
        "ProcessType": "Background",
        "EnvironmentVariables": env,
        "StandardOutPath": str(APP_HOME / "worker.log"),
        "StandardErrorPath": str(APP_HOME / "worker-error.log"),
    }
    with PLIST.open("wb") as f:
        plistlib.dump(pl, f)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], capture_output=True, text=True)
    if r.returncode:
        print("LaunchAgent note:", r.stderr.strip() or "bootstrap returned non-zero")


def build_icns(resources: Path) -> str:
    src = DEST / "assets/app-icon.png"
    resources.mkdir(parents=True, exist_ok=True)
    fallback = resources / "AppIcon.png"
    if src.exists():
        shutil.copy2(src, fallback)
    sips, iconutil = shutil.which("sips"), shutil.which("iconutil")
    if not src.exists() or not sips or not iconutil:
        return "AppIcon.png"
    with tempfile.TemporaryDirectory(prefix="codex-scheduler-icon-") as td:
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        specs = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]
        for size, scale in specs:
            pixels = size * scale
            suffix = "@2x" if scale == 2 else ""
            out = iconset / f"icon_{size}x{size}{suffix}.png"
            r = subprocess.run([sips, "-z", str(pixels), str(pixels), str(src), "--out", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode != 0:
                return "AppIcon.png"
        icns = resources / "AppIcon.icns"
        r = subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(icns)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "AppIcon.icns" if r.returncode == 0 and icns.exists() else "AppIcon.png"


def write_local_app(py: str, codex: str | None):
    shutil.rmtree(LOCAL_APP, ignore_errors=True)
    contents = LOCAL_APP / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    icon_name = build_icns(resources)
    info = {
        "CFBundleName": "Codex Scheduler",
        "CFBundleDisplayName": "Codex Scheduler",
        "CFBundleIdentifier": "com.codex.native-scheduler.local",
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "Codex Scheduler",
        "CFBundleIconFile": icon_name,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
    }
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump(info, f)
    launcher = macos / "Codex Scheduler"
    launcher.write_text(
        "#!/bin/zsh\n"
        f"export CODEX_NATIVE_SCHEDULER_HOME={shlex_quote(str(APP_HOME))}\n"
        + (f"export CODEX_BIN={shlex_quote(codex)}\n" if codex else "")
        + f"exec {shlex_quote(py)} {shlex_quote(str(DEST / 'server/local_ui_server.py'))}\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)


def main():
    if sys.platform != "darwin":
        print("This installer currently targets macOS.")
        return 2
    py = str(Path(sys.executable).resolve())
    codex = detect_codex()
    copy_plugin()
    write_mcp(py, codex)
    merge_marketplace()
    write_service(py, codex)
    write_local_app(py, codex)
    print("\n✓ Codex Scheduler v1.4.6 files installed")
    print("✓ Personal plugin marketplace updated (existing entries preserved)")
    print("✓ Background scheduler installed")
    print("✓ Quota-free local app installed:", LOCAL_APP)
    print("✓ App cover + macOS icon installed")
    print("✓ Codex binary:", codex or "not detected yet")
    print("\nQuota-free entry: open 'Codex Scheduler' from ~/Applications or Spotlight.")
    print("Optional embedded entry: restart ChatGPT manually → Plugins → Personal Local Plugins → Codex Scheduler.")
    print("Note: installer intentionally does not reopen ChatGPT, so an idle Codex thread is not immediately re-owned by Desktop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
