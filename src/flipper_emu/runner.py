"""Launch the Renode backend for the emulated Flipper Zero.

Responsibilities, all of them host-side:

* locate the Renode executable (vendored under ``tools/renode``, or ``$RENODE_EXE``)
* start the platform script with the telnet monitor enabled
* capture everything the emulator says into one log file
* stop cleanly, dumping the flash image back to disk through
  ``SystemBus.ReadBytes`` so firmware writes survive across runs

The flash dump is what makes the emulated chip behave like real silicon
(persistent settings, OTA updates) without patching the emulator: the flash lives
in a file on our side, is loaded at start-up and written back on exit.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

DEFAULT_MONITOR_PORT = 3456
DEFAULT_RUN_SECONDS = 25.0
FLASH_DUMP_SCRIPT = "flash_dump.py"


def repo_root() -> str:
    """Repository root (this file lives in ``src/flipper_emu``)."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def to_renode_path(path: str) -> str:
    """Absolute path in the forward-slash form Renode prefers."""
    return os.path.abspath(path).replace("\\", "/")


def find_renode(explicit: Optional[str] = None) -> str:
    """Return the Renode executable, or explain how to install it."""
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("RENODE_EXE"):
        candidates.append(os.environ["RENODE_EXE"])
    candidates.append(os.path.join(repo_root(), "tools", "renode", "renode.exe"))
    candidates.extend(
        sorted(glob.glob(os.path.join(repo_root(), "tools", "renode", "*", "renode.exe")))
    )
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return os.path.abspath(candidate)
    raise FileNotFoundError(
        "Renode not found. Run tools/fetch_renode.ps1 (or point RENODE_EXE at renode.exe)."
    )


@dataclass
class RunResult:
    """What happened during one emulator session."""

    log_path: str
    seconds: float
    monitor_port: int
    flash_dumped: bool
    flash_image: Optional[str]
    uart_log: Optional[str] = None

    def log_text(self) -> str:
        """Everything the emulator said (console log)."""
        with open(self.log_path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def uart_text(self) -> str:
        """The firmware's own UART output, if it was captured."""
        if not self.uart_log or not os.path.exists(self.uart_log):
            return ""
        with open(self.uart_log, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()


def write_flash_dump_script(path: str, flash_image: str) -> str:
    """Write the monitor-Python script that saves the emulated flash on exit.

    The script validates the vector table before touching the image file: if the
    platform failed to load (or the firmware is not there), the existing image
    must survive untouched rather than being overwritten with zeros.
    """
    script = "\n".join(
        [
            'print("FLASH_DUMP_START")',
            "bus = self.Machine.SystemBus",
            "try:",
            "    head = bus.ReadBytes(0x08000000, 8)",
            "    sp = head[0] | (head[1] << 8) | (head[2] << 16) | (head[3] << 24)",
            "    rv = head[4] | (head[5] << 8) | (head[6] << 16) | (head[7] << 24)",
            "    bootable = (0x20000000 < sp <= 0x20040000) and (0x08000000 <= rv < 0x080C0000) and (rv & 1)",
            "    if not bootable:",
            '        print("FLASH_DUMP_SKIPPED sp=0x%08X reset=0x%08X" % (sp, rv))',
            "    else:",
            "        data = bus.ReadBytes(0x08000000, 0x00100000)",
            '        handle = open(r"%s", "wb")' % to_renode_path(flash_image),
            "        handle.write(bytearray(data))",
            "        handle.close()",
            '        print("FLASH_DUMP_DONE sp=0x%08X reset=0x%08X" % (sp, rv))',
            "except Exception as exc:",
            '    print("FLASH_DUMP_FAILED %s" % exc)',
            "",
        ]
    )
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(script)
    return path


def build_command(
    executable: str,
    monitor_port: int,
    startup_commands: Sequence[str],
    config_path: Optional[str] = None,
    console: bool = True,
) -> List[str]:
    """Assemble the Renode command line.

    ``console=True`` runs the monitor on stdin (used by one-shot runs, which send
    pause/dump/quit that way). ``console=False`` leaves the TCP monitor listening
    on ``monitor_port`` so a long-lived session can be driven over the network -
    the two are mutually exclusive in Renode, which is worth knowing when buttons
    "cannot connect".
    """
    argv = [executable, "--disable-gui"]
    if console:
        argv.append("--console")
    argv.extend(["-P", str(monitor_port)])
    if config_path:
        argv.extend(["--config", config_path])
    for command in startup_commands:
        argv.extend(["-e", command])
    return argv


def validate_platform(
    repl_path: str,
    renode_exe: Optional[str] = None,
    timeout: float = 12.0,
) -> RunResult:
    """Load the platform description and quit, to catch `.repl` errors fast.

    This is the cheap inner loop of platform development: it never boots the
    firmware, so it finishes in seconds.
    """
    executable = find_renode(renode_exe)
    artifacts_dir = os.path.join(repo_root(), "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    log_path = os.path.join(artifacts_dir, "platform-check.log")
    argv = build_command(
        executable,
        monitor_port=-1,
        startup_commands=[
            "mach create",
            "machine LoadPlatformDescription @%s" % to_renode_path(repl_path),
            "peripherals",
            "quit",
        ],
        config_path=prepare_plugin(renode_exe),
    )
    with open(log_path, "wb") as log_handle:
        process = subprocess.Popen(
            argv,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=repo_root(),
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    return RunResult(
        log_path=log_path,
        seconds=0.0,
        monitor_port=-1,
        flash_dumped=False,
        flash_image=None,
    )


UART_CAPTURE_SCRIPT = "uart_capture.py"

#: Our C# peripheral models, built from ``src/flipper_emu/peripherals/cs``.
PERIPHERAL_ASSEMBLY = os.path.join(
    "src", "flipper_emu", "peripherals", "cs", "bin", "Release", "FlipperEmu.Peripherals.dll"
)

#: Plugin name declared by ``FlipperPeripheralsPlugin`` in that assembly.
PLUGIN_NAME = "FlipperEmu.Peripherals"

#: Generated Renode configuration for a session (the user's own is copied).
RENODE_CONFIG = "renode.config"


def peripheral_assembly_path() -> Optional[str]:
    """Path to the built peripheral assembly, or ``None`` when not built yet."""
    path = os.path.join(repo_root(), PERIPHERAL_ASSEMBLY)
    return path if os.path.exists(path) else None


def install_plugin(renode_exe: Optional[str] = None) -> bool:
    """Copy the built models next to ``renode.exe``.

    Renode discovers plugins by scanning the assemblies in its own directory
    (that is where its bundled plugins live), so the built assembly has to sit
    beside the executable. Copying is skipped when the installed copy is newer.
    """
    assembly = peripheral_assembly_path()
    if assembly is None:
        return False
    try:
        executable = find_renode(renode_exe)
    except FileNotFoundError:
        return False
    target = os.path.join(os.path.dirname(executable), os.path.basename(assembly))
    try:
        if not os.path.exists(target) or os.path.getmtime(target) < os.path.getmtime(assembly):
            shutil.copy2(assembly, target)
    except OSError:
        return False
    return os.path.exists(target)


def write_renode_config(path: str) -> str:
    """Write a Renode config that enables our plugin.

    Enabling a plugin is what makes the types in its assembly resolvable by
    platform files, and Renode activates the plugins listed in its configuration
    at start-up. The existing configuration is copied so nothing else changes.
    """
    user_config = os.path.join(os.environ.get("APPDATA", ""), "renode", "config")
    text = ""
    if os.path.exists(user_config):
        with open(user_config, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()

    if "[plugins]" in text.lower():
        lines = []
        in_plugins = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_plugins = stripped.lower().startswith("[plugins]")
            if in_plugins and stripped.startswith("enabled-plugins"):
                lines.append("enabled-plugins = %s" % PLUGIN_NAME)
                continue
            lines.append(line)
        text = "\n".join(lines) + "\n"
    else:
        text += "\n[plugins]\nenabled-plugins = %s\n" % PLUGIN_NAME

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def prepare_plugin(renode_exe: Optional[str] = None, artifacts_dir: Optional[str] = None):
    """Install the plugin and return the config path that enables it, or ``None``."""
    if not install_plugin(renode_exe):
        return None
    artifacts_dir = artifacts_dir or os.path.join(repo_root(), "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    return write_renode_config(os.path.join(artifacts_dir, RENODE_CONFIG))



def write_uart_capture_script(path: str, uart_log: str, device: str = "usart1") -> str:
    """Write the monitor-Python script that captures the firmware's UART output.

    The firmware prints its diagnostics (including ``[CRASH]``/``furi_check``
    failures) on USART1; subscribing to the model's ``CharReceived`` event is
    enough to see them without wiring a Renode backend.
    """
    script = "\n".join(
        [
            "from Antmicro import Renode",
            "import clr",
            # Try* methods carry an out-parameter, which IronPython surfaces as a
            # tuple: (success, peripheral).
            'resolved = self.TryFindPeripheralByName("%s")' % device,
            "if isinstance(resolved, tuple):",
            "    resolved = resolved[1] if len(resolved) > 1 else None",
            "if resolved is None:",
            '    print("UART_CAPTURE_FAILED no peripheral named %s")' % device,
            "else:",
            "    uart = clr.Convert(resolved, Renode.Peripherals.UART.IUART)",
            '    handle = open(r"%s", "wb", 1)' % to_renode_path(uart_log),
            # "wb" already empties the log, and it keeps the host from having to
            # truncate an append-mode handle (which raises "Unable to truncate
            # data that previously existed in a file opened in Append mode" and
            # aborts every include chained after this script).
            "    def on_char(byte):",
            "        handle.write(chr(byte))",
            "        handle.flush()",
            "    uart.CharReceived += on_char",
            '    print("UART_CAPTURE_ATTACHED %s")' % device,
            "",
        ]
    )
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(script)
    return path


def start_session(
    resc_path: str,
    monitor_port: int = DEFAULT_MONITOR_PORT,
    log_path: Optional[str] = None,
    artifacts_dir: Optional[str] = None,
    capture_uart: bool = True,
    renode_exe: Optional[str] = None,
) -> "Session":
    """Start Renode in the background and return a handle to it.

    Used by ``flipper_emu ui``: the emulator keeps running while the UI is up, and
    ``Session.stop()`` pauses it, saves the flash and quits, exactly like `run`.
    """
    executable = find_renode(renode_exe)
    artifacts_dir = artifacts_dir or os.path.join(repo_root(), "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    log_path = log_path or os.path.join(artifacts_dir, "renode-console.log")
    flash_image = os.path.join(artifacts_dir, "flash.img")
    uart_log = os.path.join(artifacts_dir, "uart1.log")

    startup = ["include @%s" % to_renode_path(resc_path)]
    if capture_uart:
        startup.append(
            "include @%s"
            % to_renode_path(write_uart_capture_script(
                os.path.join(artifacts_dir, UART_CAPTURE_SCRIPT), uart_log
            ))
        )
    argv = build_command(
        executable, monitor_port, startup, config_path=prepare_plugin(renode_exe), console=False
    )

    dump_script = write_flash_dump_script(
        os.path.join(artifacts_dir, FLASH_DUMP_SCRIPT), flash_image
    )
    log_handle = open(log_path, "wb")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=repo_root(),
    )
    return Session(process, log_handle, log_path, monitor_port, dump_script)


class Session:
    """A running emulator session, controllable from the host."""

    def __init__(
        self,
        process: "subprocess.Popen",
        log_handle,
        log_path: str,
        monitor_port: int,
        dump_script: str,
    ) -> None:
        self.process = process
        self.log_handle = log_handle
        self.log_path = log_path
        self.monitor_port = monitor_port
        self.dump_script = dump_script

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def wait_for_stream(self, path: str, timeout: float = 30.0) -> bool:
        """Wait until the display recorder file exists and has content."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path) and os.path.getsize(path) > len(b"FZDPI1\n") + 64:
                return True
            if not self.running:
                return False
            time.sleep(0.25)
        return False

    def stop(self, dump_flash: bool = True, timeout: float = 25.0) -> None:
        """Pause, save the flash and quit - over the TCP monitor if it is up."""
        if self.process.poll() is None:
            stopped = False
            try:
                from .console.monitor import RenodeMonitor

                monitor = RenodeMonitor(port=self.monitor_port, timeout=5.0)
                if monitor.connect(attempts=4, delay=0.5):
                    monitor.command("pause", wait=0.3)
                    if dump_flash:
                        monitor.command(
                            "include @%s" % to_renode_path(self.dump_script), wait=0.3
                        )
                    monitor.command("quit", wait=0.3)
                    monitor.close()
                    stopped = True
                self.process.wait(timeout=timeout)
            except Exception:
                stopped = False
            if not stopped:
                try:
                    self.process.stdin.write(b"pause\n")
                    self.process.stdin.write(b"quit\n")
                    self.process.stdin.flush()
                    self.process.wait(timeout=timeout)
                except Exception:
                    self.process.kill()
        try:
            self.process.stdin.close()
        except Exception:
            pass
        try:
            self.log_handle.close()
        except Exception:
            pass


def run(
    resc_path: str,
    seconds: float = DEFAULT_RUN_SECONDS,
    monitor_port: int = DEFAULT_MONITOR_PORT,
    log_path: Optional[str] = None,
    flash_image: Optional[str] = None,
    dump_flash: bool = True,
    extra_commands: Sequence[str] = (),
    renode_exe: Optional[str] = None,
    artifacts_dir: Optional[str] = None,
    capture_uart: bool = True,
    uart_log: Optional[str] = None,
) -> RunResult:
    """Boot the firmware in Renode for ``seconds`` and return a run report."""
    executable = find_renode(renode_exe)
    artifacts_dir = artifacts_dir or os.path.join(repo_root(), "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    log_path = log_path or os.path.join(artifacts_dir, "renode-console.log")
    flash_image = flash_image or os.path.join(artifacts_dir, "flash.img")
    uart_log = uart_log or os.path.join(artifacts_dir, "uart1.log")

    startup = ["include @%s" % to_renode_path(resc_path)]
    if capture_uart:
        capture_script = write_uart_capture_script(
            os.path.join(artifacts_dir, UART_CAPTURE_SCRIPT), uart_log
        )
        startup.append("include @%s" % to_renode_path(capture_script))
    startup.extend(extra_commands)
    argv = build_command(executable, monitor_port, startup, config_path=prepare_plugin(renode_exe))

    dump_script = write_flash_dump_script(
        os.path.join(artifacts_dir, FLASH_DUMP_SCRIPT), flash_image
    )

    started = time.time()
    with open(log_path, "wb") as log_handle:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=repo_root(),
        )
        try:
            time.sleep(seconds)
            if process.poll() is None:
                # Pause, snapshot the flash, then leave the monitor gracefully.
                process.stdin.write(b"pause\n")
                if dump_flash:
                    process.stdin.write(("include @%s\n" % to_renode_path(dump_script)).encode())
                process.stdin.write(b"quit\n")
                process.stdin.flush()
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.stdin.close()
            except Exception:
                pass

    log_text = ""
    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        log_text = handle.read()
    return RunResult(
        log_path=log_path,
        seconds=time.time() - started,
        monitor_port=monitor_port,
        flash_dumped="FLASH_DUMP_DONE" in log_text,
        flash_image=flash_image if dump_flash else None,
        uart_log=uart_log if capture_uart else None,
    )
