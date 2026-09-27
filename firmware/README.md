Where to put firmware packages (`.dfu`, `.bin`, `.elf`). Files here are
git-ignored; nothing in this directory is required for the tests except
`flipper-z-f7-full-1.4.3.dfu`, which the loader is pinned to.

Fetch official releases, for example:

```powershell
Invoke-WebRequest `
  https://update.flipperzero.one/builds/firmware/1.4.3/flipper-z-f7-full-1.4.3.dfu `
  -OutFile flipper-z-f7-full-1.4.3.dfu
```

The matching `-f7-firmware-1.4.3.elf` from the same release is useful for
symbolising traces and is worth keeping alongside it. The SHA256 published in
Flipper's `update.flipperzero.one/firmware/directory.json` is what the loader's
integrity check should be compared against (the package's own DFU CRC field does
not follow the DFU convention, see `docs/BRINGUP_LOG.md`).
