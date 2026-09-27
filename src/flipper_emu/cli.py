"""Command line entry point.

    py -3.9 -m flipper_emu bus        # print the emulated address map
    py -3.9 -m flipper_emu platform   # regenerate the Renode platform files
    py -3.9 -m flipper_emu load       # build/refresh the flash image from a .dfu
    py -3.9 -m flipper_emu run        # boot the firmware and report what is missing
    py -3.9 -m flipper_emu report     # re-analyse a previous run log
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Optional

from . import runner
from .console import trace
from .fw import flash_image, memmap
from .platform import generate

DEFAULT_RESC = os.path.join("platform", "flipper_zero.resc")
DEFAULT_FLASH = os.path.join("artifacts", "flash.img")
FIRMWARE_DIR = "firmware"


def default_dfu() -> Optional[str]:
    """Newest ``.dfu`` package found in ``firmware/``."""
    directory = os.path.join(runner.repo_root(), FIRMWARE_DIR)
    if not os.path.isdir(directory):
        return None
    candidates = sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.lower().endswith(".dfu")
    )
    return candidates[-1] if candidates else None


def cmd_bus(args: argparse.Namespace) -> int:
    print(memmap.describe())
    issues = memmap.overlap_issues() + memmap.alignment_issues(memmap.PERIPHERALS)
    print("dispatch issues: %s" % (issues or "none"))
    return 0


def cmd_platform(args: argparse.Namespace) -> int:
    paths = generate.write_platform(args.out_dir, args.flash)
    for key in sorted(paths):
        print("%-9s %s" % (key, paths[key]))
    print(
        "bus: %d peripherals (%d modelled, %d stubbed)"
        % (
            len(memmap.PERIPHERALS),
            len(memmap.modelled_peripherals()),
            len(memmap.stubbed_peripherals()),
        )
    )
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    dfu_path = args.dfu or default_dfu()
    if dfu_path is None:
        print("no .dfu package given and none found in %s/" % FIRMWARE_DIR, file=sys.stderr)
        return 2
    flash, image = flash_image.load_or_create(
        args.flash, dfu_path, verify_crc=args.verify_crc, rebuild=args.rebuild
    )
    if image is not None:
        print(image.describe())
    print(flash.describe())
    print("flash image: %s" % args.flash)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    extra_commands = list(args.renode_command or ())
    for script in args.renode_include or ():
        extra_commands.append("include @%s" % runner.to_renode_path(script))
    result = runner.run(
        args.resc,
        seconds=args.seconds,
        monitor_port=args.monitor_port,
        log_path=args.log,
        flash_image=args.flash,
        dump_flash=not args.no_dump,
        renode_exe=args.renode,
        extra_commands=extra_commands,
    )
    print("emulator session: %.1f s, log %s" % (result.seconds, result.log_path))
    print("flash dump on exit: %s" % ("yes" if result.flash_dumped else "no"))
    uart_text = result.uart_text()
    if uart_text.strip():
        lines = [line for line in uart_text.splitlines() if line.strip()]
        print("firmware UART output: %d line(s), last %d:" % (len(lines), min(15, len(lines))))
        for line in lines[-15:]:
            print("  | %s" % line.strip()[:200])
        crashes = [line for line in lines if "CRASH" in line or "furi_check" in line]
        if crashes:
            print("  crash reports: %d" % len(crashes))
            for line in crashes[:5]:
                print("  ! %s" % line.strip()[:200])
    else:
        print("firmware UART output: none captured")
    report = trace.analyse(result.log_text())
    print(report.render())
    missing = report.missing_peripherals()
    if missing:
        print("touched but not implemented yet:")
        for name in missing:
            print("  - %s" % name)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Start the emulator, then open the live window (or run the self-test)."""
    from .frontend import ui_tk

    stream = os.path.join(runner.repo_root(), "artifacts", "display-stream.bin")
    session = None
    if not args.attach:
        session = runner.start_session(
            args.resc, monitor_port=args.monitor_port, renode_exe=args.renode
        )
        print("emulator started (monitor port %d, log %s)" % (session.monitor_port, session.log_path))
        print("waiting for the firmware to draw...")
        if not session.wait_for_stream(stream, timeout=args.timeout):
            print("warning: no display stream yet (continuing anyway)", file=sys.stderr)

    ui_args = [
        "--stream",
        stream,
        "--host",
        args.host,
        "--monitor-port",
        str(args.monitor_port),
        "--scale",
        str(args.scale),
    ]
    if args.no_buttons:
        ui_args.append("--no-buttons")
    if args.selftest:
        ui_args.extend(["--selftest", str(args.selftest)])

    try:
        return ui_tk.main(ui_args)
    finally:
        if session is not None:
            session.stop()
            print("emulator stopped, flash saved")


def cmd_check(args: argparse.Namespace) -> int:
    """Load the generated platform into Renode and report any complaint."""
    result = runner.validate_platform(args.repl, args.renode)
    text = trace.strip_ansi(result.log_text())
    lines = text.splitlines()
    errors = [
        line.strip()
        for line in lines
        if re.search(r"(?i)(error E\d+|syntax error|error executing command|unhandled exception|File does not exist)",
                     line)
    ]
    loaded = "System bus created" in text
    peripherals = [
        line.strip() for line in lines if line.strip().startswith(("\u251c", "\u2514", "`--", "|--"))
    ]
    print("platform:    %s" % args.repl)
    print("load result: %s" % ("ok" if loaded and not errors else "FAILED"))
    if peripherals:
        print("peripherals instantiated: %d" % len(peripherals))
    for line in errors[:12]:
        print("  %s" % line)
    return 0 if (loaded and not errors) else 1


def cmd_report(args: argparse.Namespace) -> int:
    with open(args.log, "r", encoding="utf-8", errors="replace") as handle:
        report = trace.analyse(handle.read())
    print(report.render(limit=args.limit))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flipper_emu", description=__doc__)
    parser.add_argument("--flash", default=DEFAULT_FLASH, help="path to the flash image")
    subparsers = parser.add_subparsers(dest="verb")

    bus = subparsers.add_parser("bus", help="print the emulated address map")
    bus.set_defaults(func=cmd_bus)

    platform = subparsers.add_parser("platform", help="regenerate the platform files")
    platform.add_argument("--out-dir", default=None)
    platform.set_defaults(func=cmd_platform)

    load = subparsers.add_parser("load", help="build the flash image from a .dfu package")
    load.add_argument("--dfu", default=None, help="path to the .dfu package")
    load.add_argument("--rebuild", action="store_true", help="overwrite an existing flash image")
    load.add_argument("--verify-crc", action="store_true", help="verify the DFU suffix CRC")
    load.set_defaults(func=cmd_load)

    run = subparsers.add_parser("run", help="boot the firmware in the emulator")
    run.add_argument("--resc", default=DEFAULT_RESC, help="Renode start-up script")
    run.add_argument("--seconds", type=float, default=runner.DEFAULT_RUN_SECONDS)
    run.add_argument("--monitor-port", type=int, default=runner.DEFAULT_MONITOR_PORT)
    run.add_argument("--log", default=None, help="where to write the emulator log")
    run.add_argument("--renode", default=None, help="path to renode.exe")
    run.add_argument("--no-dump", action="store_true", help="do not save the flash on exit")
    run.add_argument(
        "--renode-command",
        action="append",
        default=None,
        help="extra Renode monitor command to run at start-up (repeatable). Note that "
        "a command containing spaces needs shell quoting; prefer --renode-include.",
    )
    run.add_argument(
        "--renode-include",
        action="append",
        default=None,
        help="monitor script to load at start-up (repeatable), e.g. an instrumented "
        "probe; expands to 'include @<path>'",
    )
    run.set_defaults(func=cmd_run)

    report = subparsers.add_parser("report", help="analyse an emulator log")
    report.add_argument("log", help="path to the log file")
    report.add_argument("--limit", type=int, default=25)
    report.set_defaults(func=cmd_report)

    check = subparsers.add_parser("check", help="load the platform in Renode and report errors")
    check.add_argument("--repl", default=os.path.join("platform", "stm32wb55_flipper.repl"))
    check.add_argument("--renode", default=None, help="path to renode.exe")
    check.set_defaults(func=cmd_check)

    ui = subparsers.add_parser(
        "ui", help="start the emulator and open the live 128x64 window (keyboard = buttons)"
    )
    ui.add_argument("--resc", default=DEFAULT_RESC, help="Renode start-up script")
    ui.add_argument("--renode", default=None, help="path to renode.exe")
    ui.add_argument("--monitor-port", type=int, default=runner.DEFAULT_MONITOR_PORT)
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--scale", type=int, default=6, help="window scale (6 gives 768x384)")
    ui.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the first frame")
    ui.add_argument("--attach", action="store_true", help="do not start Renode; attach to a running one")
    ui.add_argument("--no-buttons", action="store_true", help="do not connect to the monitor")
    ui.add_argument(
        "--selftest",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="headless check: tail for SECONDS, inject a button, report the result",
    )
    ui.set_defaults(func=cmd_ui)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
