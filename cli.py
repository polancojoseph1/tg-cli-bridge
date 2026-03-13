"""bridgebot CLI — install and manage bot instances as persistent background services.

Supports:
  macOS  — LaunchAgents (~~/Library/LaunchAgents/*.plist)
  Linux  — systemd user units (~/.config/systemd/user/*.service)
  Windows — prints manual start command (Task Scheduler setup is manual)
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# macOS — launchd plist
# ---------------------------------------------------------------------------

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>server:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{project_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{home}</string>
        <key>PATH</key>
        <string>{homebrew_prefix}/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PYTHONPATH</key>
        <string>{project_dir}/.venv/lib/python{pyver}/site-packages</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{err}</string>
</dict>
</plist>
"""

# ---------------------------------------------------------------------------
# Linux — systemd user unit
# ---------------------------------------------------------------------------

SYSTEMD_TEMPLATE = """\
[Unit]
Description=bridgebot ({name})
After=network.target

[Service]
Type=simple
WorkingDirectory={project_dir}
ExecStart={python} -m uvicorn server:app --host 0.0.0.0 --port {port}
Restart=always
RestartSec=10
Environment=HOME={home}
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=PYTHONPATH={project_dir}/.venv/lib/python{pyver}/site-packages
StandardOutput=append:{log}
StandardError=append:{err}

[Install]
WantedBy=default.target
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _homebrew_prefix() -> str:
    """Return Homebrew prefix (e.g. /opt/homebrew on Apple Silicon, /usr/local on Intel).
    Returns empty string on non-macOS systems."""
    if sys.platform != "darwin":
        return ""
    result = subprocess.run(["brew", "--prefix"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return "/opt/homebrew"  # sensible fallback


def _venv_python(project_dir: Path) -> str:
    """Return the venv Python path for the current OS."""
    if sys.platform == "win32":
        p = project_dir / ".venv" / "Scripts" / "python.exe"
    else:
        p = project_dir / ".venv" / "bin" / "python"
    return str(p) if p.exists() else sys.executable


# ---------------------------------------------------------------------------
# macOS install
# ---------------------------------------------------------------------------

def _install_macos(name: str, port: str, project_dir: Path) -> None:
    python = _venv_python(project_dir)
    home = str(Path.home())
    pyver = _python_version()

    logs_dir = Path.home() / "Library" / "Logs" / "bridgebot"
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    logs_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    label = f"bridgebot.{name}"
    plist_path = agents_dir / f"{label}.plist"
    log_path = logs_dir / f"{name}.log"
    err_path = logs_dir / f"{name}.err.log"

    plist_path.write_text(PLIST_TEMPLATE.format(
        label=label, python=python, port=port,
        project_dir=str(project_dir), home=home, pyver=pyver,
        log=str(log_path), err=str(err_path),
        homebrew_prefix=_homebrew_prefix(),
    ))
    print(f"Wrote: {plist_path}")

    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Loaded: {label}")
        print(f"Logs:   {log_path}")
        print(f"        {err_path}")
        print(f"\nTo stop:    launchctl unload {plist_path}")
        print(f"To restart: launchctl unload {plist_path} && sleep 2 && launchctl load {plist_path}")
    else:
        print(f"launchctl load failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


def _uninstall_macos(name: str) -> None:
    label = f"bridgebot.{name}"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    if plist_path.exists():
        plist_path.unlink()
        print(f"Removed: {plist_path}")
    else:
        print(f"Plist not found: {plist_path}")
    print(f"Uninstalled: {label}")


def _list_macos() -> None:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    plists = sorted(agents_dir.glob("bridgebot.*.plist"))
    if not plists:
        print("No bridgebot instances installed.")
        return
    print(f"{'NAME':<20} PLIST")
    for p in plists:
        name = p.stem.removeprefix("bridgebot.")
        print(f"{name:<20} {p}")


# ---------------------------------------------------------------------------
# Linux install
# ---------------------------------------------------------------------------

def _install_linux(name: str, port: str, project_dir: Path) -> None:
    python = _venv_python(project_dir)
    home = str(Path.home())
    pyver = _python_version()

    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    logs_dir = Path.home() / ".local" / "share" / "bridgebot" / "logs"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    unit_name = f"bridgebot-{name}.service"
    unit_path = systemd_dir / unit_name
    log_path = logs_dir / f"{name}.log"
    err_path = logs_dir / f"{name}.err.log"

    unit_path.write_text(SYSTEMD_TEMPLATE.format(
        name=name, python=python, port=port,
        project_dir=str(project_dir), home=home, pyver=pyver,
        log=str(log_path), err=str(err_path),
    ))
    print(f"Wrote: {unit_path}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", unit_name], check=True)
    print(f"Enabled and started: {unit_name}")
    print(f"Logs: journalctl --user -u {unit_name} -f")
    print(f"      or: {log_path}")
    print(f"\nTo stop:    systemctl --user stop {unit_name}")
    print(f"To restart: systemctl --user restart {unit_name}")


def _uninstall_linux(name: str) -> None:
    unit_name = f"bridgebot-{name}.service"
    unit_path = Path.home() / ".config" / "systemd" / "user" / unit_name
    subprocess.run(["systemctl", "--user", "disable", "--now", unit_name], capture_output=True)
    if unit_path.exists():
        unit_path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        print(f"Removed: {unit_path}")
    else:
        print(f"Unit not found: {unit_path}")
    print(f"Uninstalled: {unit_name}")


def _list_linux() -> None:
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    units = sorted(systemd_dir.glob("bridgebot-*.service"))
    if not units:
        print("No bridgebot instances installed.")
        return
    print(f"{'NAME':<20} UNIT FILE")
    for u in units:
        name = u.stem.removeprefix("bridgebot-")
        print(f"{name:<20} {u}")


# ---------------------------------------------------------------------------
# Windows — Task Scheduler via schtasks (built-in, no extra install needed)
# ---------------------------------------------------------------------------

def _task_name(name: str) -> str:
    return f"bridgebot-{name}"


def _install_windows(name: str, port: str, project_dir: Path) -> None:
    python = _venv_python(project_dir)
    # Use pythonw.exe to suppress the console window on startup
    pythonw = Path(python).parent / "pythonw.exe"
    runner = str(pythonw) if pythonw.exists() else python

    task_name = _task_name(name)
    args = f'-m uvicorn server:app --host 0.0.0.0 --port {port}'

    # schtasks /Create — runs at logon, hidden, in the project directory
    result = subprocess.run([
        "schtasks", "/Create", "/F",
        "/TN", task_name,
        "/TR", f'"{runner}" {args}',
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/IT",           # only when user is logged in (interactive)
    ], capture_output=True, text=True)

    if result.returncode == 0:
        print(f"Scheduled task created: {task_name}")
        # Start it now too
        subprocess.run(["schtasks", "/Run", "/TN", task_name], capture_output=True)
        print(f"Started: {task_name}")
        print()
        print("The bot will auto-start on next login.")
        print()
        print(f"To stop:    schtasks /End /TN {task_name}")
        print(f"To start:   schtasks /Run /TN {task_name}")
        print(f"To remove:  schtasks /Delete /F /TN {task_name}")
        print()
        _print_nssm_tip(name, runner, args, project_dir)
    else:
        print(f"schtasks failed: {result.stderr.strip()}", file=sys.stderr)
        print("Falling back to manual instructions:")
        print(f"  {runner} {args}")
        print(f"  (run from: {project_dir})")


def _print_nssm_tip(name: str, runner: str, args: str, project_dir: Path) -> None:
    """Print optional NSSM tip for crash-restart support."""
    if subprocess.run(["where", "nssm"], capture_output=True).returncode == 0:
        # NSSM is installed — offer to use it
        print("NSSM detected! For crash-restart support, run:")
        print(f'  nssm install {_task_name(name)} "{runner}" "{args}"')
        print(f'  nssm set {_task_name(name)} AppDirectory "{project_dir}"')
        print(f'  nssm start {_task_name(name)}')
    else:
        print("Tip: install NSSM for automatic crash-restart:")
        print("  https://nssm.cc/download  (free, ~300KB)")
        print(f'  nssm install {_task_name(name)}  — then point it at:')
        print(f'  Program: {runner}')
        print(f'  Arguments: {args}')
        print(f'  Start in: {project_dir}')


def _uninstall_windows(name: str) -> None:
    task_name = _task_name(name)
    subprocess.run(["schtasks", "/End", "/TN", task_name], capture_output=True)
    result = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", task_name],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"Removed scheduled task: {task_name}")
    else:
        # Try NSSM too
        subprocess.run(["nssm", "stop", task_name], capture_output=True)
        subprocess.run(["nssm", "remove", task_name, "confirm"], capture_output=True)
        print(f"Uninstalled: {task_name}")


def _list_windows() -> None:
    result = subprocess.run(
        ["schtasks", "/Query", "/FO", "LIST", "/NH"],
        capture_output=True, text=True
    )
    found = []
    for line in result.stdout.splitlines():
        if "bridgebot-" in line:
            found.append(line.strip())
    if not found:
        print("No bridgebot instances found in Task Scheduler.")
        return
    print(f"{'NAME':<30} TASK")
    for entry in found:
        name = entry.split("bridgebot-")[-1].strip()
        print(f"{name:<30} {entry}")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def cmd_install(args) -> None:
    project_dir = Path(__file__).parent.resolve()
    if sys.platform == "darwin":
        _install_macos(args.name, args.port, project_dir)
    elif sys.platform.startswith("linux"):
        _install_linux(args.name, args.port, project_dir)
    else:
        _install_windows(args.name, args.port, project_dir)


def cmd_uninstall(args) -> None:
    if sys.platform == "darwin":
        _uninstall_macos(args.name)
    elif sys.platform.startswith("linux"):
        _uninstall_linux(args.name)
    else:
        _uninstall_windows(args.name)


def cmd_list(args) -> None:
    if sys.platform == "darwin":
        _list_macos()
    elif sys.platform.startswith("linux"):
        _list_linux()
    else:
        _list_windows()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bridgebot",
        description="Manage bridgebot bot instances as persistent background services",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    install_p = sub.add_parser("install", help="Install a named bot instance as a background service")
    install_p.add_argument("--name", required=True, help="Instance name (e.g. claude, gemini)")
    install_p.add_argument("--port", default=os.environ.get("PORT", "8588"), help="Port to run uvicorn on (default: PORT env var or 8588)")

    uninstall_p = sub.add_parser("uninstall", help="Remove a named service instance")
    uninstall_p.add_argument("--name", required=True, help="Instance name to remove")

    sub.add_parser("list", help="List installed bridgebot service instances")

    args = parser.parse_args()

    if args.command == "install":
        cmd_install(args)
    elif args.command == "uninstall":
        cmd_uninstall(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
