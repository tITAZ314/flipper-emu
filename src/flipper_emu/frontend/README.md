# Frontend (UI and rendering) — planned

Nothing is implemented here yet. The design, so it can be built against a stable
contract:

| File | Responsibility |
|---|---|
| `displaysink.py` | Frame source abstraction: whatever produces a 128×64 monochrome framebuffer (today: the C# `St7567Display` model writing a frame file; later: a socket or the monitor), plus PPM/PNG decoding in pure Python. |
| `keymap.py` | Key → button mapping: arrows → UP/LEFT/RIGHT/DOWN, `Enter`/`Space` → OK, `Esc`/`Backspace` → BACK. |
| `ui_tk.py` | `tkinter` window showing the framebuffer scaled 6× (768×384) via `PhotoImage` + `zoom()`, keyboard events mapped through `keymap.py`. Standard library only — no install step. |

## Button injection

Buttons are GPIO levels, and the firmware reads both the EXTI edge and the pin
level, so the UI must produce a real level transition and hold it while the key
is down. Two routes, both already proven to exist in Renode 1.17:

1. monitor command invoking the port's GPIO input method (`gpio OnGPIO <pin>
   <value>` is the documented pattern), reached over the telnet monitor that
   `flipper_emu.runner` already enables (`-P <port>`);
2. a small C# `FlipperButtons` peripheral exposing `Press`/`Release` monitor
   commands, if the port's method turns out not to be callable directly.

The frame path is deliberately decoupled from the button path, so the UI can be
developed against recorded frames (`artifacts/`) before the display model exists.

## Seeing the first-start slideshow in the window

`flipper_emu ui` boots the generated platform script and nothing else, and that script
leaves the card out of its slot (`gpioPortC OnGPIO 10 true` = SD card detect high).
With no card the firmware never mounts storage, so `storage_file_exists("/int/.slideshow")`
is false, the desktop never queues its slideshow scene, and the window shows the idle
animation. Present the card to get the slideshow:

```powershell
py -3.9 src/flipper_emu/platform/sdcard_build.py   # rebuild first: a run deletes the file
py -3.9 -m flipper_emu ui --card
```

`--card` includes `src/flipper_emu/platform/card_present.resc` after the platform
script's `start` (exactly what `run --card`, or
`run --renode-include src/flipper_emu/platform/card_present.resc`, does), and `ui`
takes `--renode-include` too, so a live session can carry the probes from `_renode/`.
The `/.int/.slideshow` file is deleted by the firmware when the slideshow exits, so it
has to be rebuilt before each run that should show it.

## When the keys do nothing

`ui` sends `gpioPortB OnGPIO 12 false` over Renode's telnet monitor, so three things can
silently swallow a key press, and each one now reports itself:

1. **A command Renode refuses.** `console.monitor.check_reply` looks at the reply and
   `ButtonInjector` raises `MonitorError` (shown in the window's status line) instead of
   dropping it on the floor.
2. **Another Renode holding the monitor port.** An orphaned instance then receives every
   button command while the window keeps showing frames (the panel stream is a file), so
   `cli.free_monitor_port` picks the first *bindable* port and says so.
3. **Start-up commands that never ran.** Renode joins several `-e` arguments into one
   line and telnet mode fails to tokenize that line, which used to drop `--card`, the
   UART capture and any `--renode-include`.  `runner.write_boot_script` now writes one
   `session_boot.resc` holding them all, passed as a single `-e`.

`py -3.9 -m flipper_emu ui --card --selftest 25` checks the path from the firmware's
side: it arms hooks over the monitor on `input_isr` (0x080827E8) and `view_port_input`
(0x08082500), taps OK, and reads the markers back out of the session log.  The report
says which stage is missing - "never reached the input service" (the wire/port), or
"the gui never delivered it to a view port" (the emulator).  Before that the self-test
only required the panel to change, which the idle animation does by itself.
