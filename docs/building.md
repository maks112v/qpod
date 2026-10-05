# Build and validation

| Path      | Holds                                                                        |
| --------- | ---------------------------------------------------------------------------- |
| `patch/`  | The firmware payload (C, MIPS assembly, linker script) and the iPod UI audit |
| `tools/`  | Build, port and release scripts                                              |
| `test/`   | Host and MIPS emulator tests, each runnable as `python3 test/test_*.py`      |
| `assets/` | Boot splash, added icons (`icons/`) and the README banner                    |
| `docs/`   | Documentation                                                                |

Rebuilt firmware uses `assets/boot-logo.jpg` as the boot splash by default. Pass another 320x375 JPEG with `--logo` to use your own; see [boot.md](boot.md).

Requires clang/lld/llvm-objcopy, squashfs-tools 4.6 or later (tested 4.6.1 and 4.7.5), ImageMagick 6 or 7 for the iPod UI's settings icons (`convert` or `magick`; tested 6.9.12), the test harness dependencies in `requirements.txt`, and the original ZIP:

```text
154c17822d09be001be35c03d2d3488424dee195221790bd70864480d55b0f00
```

SHA-256 of the stock Shanling Q2 V1.32 firmware ZIP.

```sh
python3 tools/build.py 'Q2 Firmware V1.32.zip' --out /tmp/q2-build
python3 tools/build.py 'Q2 Firmware V1.32.zip' --out /tmp/q2-ipod --ipod
python3 tools/build.py 'Q2 Firmware V1.32.zip' --out /tmp/q2-dev --ipod --dev  # iPod test build
python3 test/peq.py  # PEQ parser/storage, DSP, editor and player checks, and the scrobble upload (host cc; player needs -m32 libs, upload libcrypto)
python3 test/coverflow.py  # Coverflow art cache and depth renderer (host cc -m32, pthreads)
python3 test/coverflow.py --captures /tmp/cf  # the same, plus the renderer's frames as PNGs
python3 test/build.py  # JPEG header checks; no emulator required
python3 test/build.py 'Q2 Firmware V1.32.zip'  # optional packaging/reproducibility checks
python3 test/patch.py /tmp/q2-build  # after: pip install -r requirements.txt
python3 tools/emulator.py /tmp/q2-ipod  # visual emulator from a build directory
python3 tools/emulator.py Q2.Firmware.V7.7.zip  # or directly from a packaged iPod release
python3 tools/port.py 'Q2 Firmware V1.32.zip' --self-check  # every raw stock address has a signature (internals.md#porting)
```

The visual emulator runs the built MIPS payload and the stock callbacks used by the test harness.
Click its window to capture input. Two-finger vertical scrolling on a macOS trackpad sends the same
key-down-before and key-up-before pair as one Q2 wheel tick, including the real timing used by the
overshoot filter and acceleration. A click in the left, middle or right third sends the previous,
centre or next hardware button. Escape releases input; Return captures it again. The arrow keys and
Space remain available as previous, next and Play/Pause controls while input is captured. Up and
Down send wheel ticks; Backspace sends the Q2 Return key.

This is an application emulator, not a full Shanling SoC emulator. It executes the real MIPS input,
navigation, animation and paint hooks, while the existing harness supplies the framebuffer, widget,
storage, audio and network boundaries. Device validation is still required for kernel drivers, DAC,
Bluetooth, framebuffer page flipping and physical wheel electrical behavior.

Both variants replace the stock equalizer page with a 30-band PEQ editor (bands, shelves, preamp, on/off, presets, `/EQ` import) and patch `hciplayer`'s equalizer filter with the matching DSP. Both also clear the 44.1 kHz AAC capability bit in `bluealsa` (see [internals.md](internals.md#bluetooth-aac)).

The updater compares `firmware_v20.info`'s version with demo's one version literal and refuses only an identical one (`update_firmware`, `0x4f8440`; the online check `check_otginfo`, `0x4f8968`). So both carry a 5-character build tag, `V<version>S` (Stock) or `V<version>I` (iPod): the mod installs over stock and over any other build, and stock V1.32 installs back over it. About's **FW. Version** row reads the same literal, so `ringnav_about` shows the stock firmware's version there (`STOCK_VERSION`, from the stock `firmware_v20.info`) and adds a **Q2 Pod** row below it, `V<version> iPod` or `V<version> Stock`.

`--dev` tags a build with the release tag in lowercase (`V<version>s`/`V<version>i`, and `dev` after the Q2 Pod row's edition), so a test unit is distinguishable from the release and the updater installs the release over it. It applies to that build only: the release procedure never passes `--dev`, and the manifest records `dev: true`.

The suite executes the actual patched MIPS payload and stock key/touch filters. Coverflow's depth renderer also runs as MIPS and must draw byte for byte what the host build of the same source draws; the suite prints its instruction count per frame, which is a relative measure only, not a frame time. UI services are mocked; carousel checks execute native animator parameter writes and stock completion, with a deterministic animation scheduler, and audit the stock creation path. Separate scenarios execute the stock canvas clip/color/rectangle code and the stock rounded fill/stroke entry points down to mocked LCD and vgcanvas sinks. The iPod wheel scenarios deliver each tick as the pair stock posts, key-down-before through the hook and the stock callback and then key-up-before, leave the stock latches as stock does, and count Key Tone clicks as the `system()` commands the real `buzzeer_switch` issues, the one mocked boundary; the stock binary runs the same presses for comparison. Case coverage lives in `test/patch.py`.

The CPU-LCD fill path runs end to end down to mocked LCD sinks, including radius clamping, the `radius <= 2` decline and allocation balance. The stock rounded vgcanvas branch could not be executed end to end under Unicorn 2.1.4: the stock binary is built `-mfp64` and Unicorn's MIPS32 FPU only implements `FR=0`, so its 64-bit conversions trap. The test harness runs the branch up to the first such instruction and asserts the vgcanvas color and line-width calls that precede it.

The builder also rejects any `patch/contexts.inc` name that is not a window name in the stock rootfs UI assets, so an allowlist typo cannot silently disable a screen. Widget field offsets and shared ABI constants live in `patch/offsets.inc`; the payload and the test mocks read the same file.

The test runner refuses a `manifest.json` whose `source_sha256` does not match the current patch sources, and verifies the stock executable, patched executable and payload hashes against that manifest, so stale or mixed artifacts cannot pass as the current build.

A MIPS instruction-count regression check verifies that filling the position table does not increase steady paint or wheel work in the current scope. These are mocked-service instruction counts, not hardware latency measurements.

Two fresh builds must produce identical `update.tar` files. Packaging verifies MD5 entries, unchanged kernel and rootfs metadata (the Stock build's Coverflow card icons are the only added inodes, with `menu_music`'s metadata; the stock EQ preset page and the images only the stock EQ pages show are removed, and in iPod also the 14 Home carousel images; iPod's 40-pixel settings icons replace the stock files in place, see [ipod.md](ipod.md#settings-icons)), and a rootfs no larger than stock. The icons are 32-colour palette PNGs and the payload is built with `-Oz` because of that limit, and demo's `.pdr` section (MIPS procedure descriptors, 423 KB past every LOAD segment, never read at run time; `PDR` in `tools/build.py`) ships zeroed, which packs to almost nothing: Stock has about 58 KB of rootfs space left, iPod about 300 KB (its 40-pixel settings icons are Lanczos-filtered RGBA, about 14 KB more than the stock 52-pixel ones).
