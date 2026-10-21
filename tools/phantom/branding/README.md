# Phantom branding and sound generators

These scripts regenerate Phantom's branded files in this repository. Edit a script's
settings (colors, sizes, sound recipes), run it, rebuild Phantom, and commit the results.
Run them from anywhere: they find the repository from their own location.

| Script | Writes | Needs |
|---|---|---|
| `make_icons.py [image]` | `release/windows/icons/winblender.ico` (plus extra sizes in `out/`) | Pillow |
| `make_ui_logos.py` | `release/datafiles/icons_svg/blender.svg`, `blender_logo_large.svg` | Python only |
| `make_splash.py` | `release/datafiles/splash.png` | Pillow, Windows' Bahnschrift font |
| `make_theme.py` | `release/datafiles/userdef/userdef_default_theme.c` | git, an `upstream` remote |
| `export_theme_preset.py` | `scripts/presets/interface_theme/Phantom.xml` | a built Phantom |
| `make_sounds.py [dir]` | `release/datafiles/sounds/*.wav` | Python only |

`phantom_source.webp` is the app icon artwork that `make_icons.py` and `make_splash.py` use.

## Typical changes

- **New icon:** replace `phantom_source.webp`, then run `make_icons.py` and `make_splash.py`.
- **Theme colors or sizes:** edit the palette at the top of `make_theme.py` and run it. It always
  starts from Blender's own default theme (`upstream/main`), so it can be re-run safely. Rebuild,
  then refresh the bundled preset:

  ```
  phantom.exe --background --factory-startup --python export_theme_preset.py
  ```

- **Sounds:** each sound is a small recipe function in `make_sounds.py` (`click`, `confirm`,
  `type_tap`, `loop_cut`, ...). Change one and re-run; the output is deterministic.

Pillow can be installed with `python -m pip install Pillow`.
