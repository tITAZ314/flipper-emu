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
