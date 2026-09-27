"""Software-only Flipper Zero emulator (Renode + STM32WB55 platform).

Module boundaries:

* ``fw``          - firmware loader: DfuSe parsing, flash image assembly, memory map
* ``platform``    - emulated bus description (Renode ``.repl``/``.resc`` generation)
* ``peripherals`` - peripheral models (IronPython 2.7 register models + C# bus devices)
* ``frontend``    - user interface and rendering
* ``console``     - debug console: register traces, unimplemented-access reports, faults
"""

__version__ = "0.1.0"
