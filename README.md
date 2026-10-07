<p align="center">
  <a href="https://youtu.be/C5x05EwsPGI" title="Watch the Q2 Pod video on YouTube">
    <img src="assets/banner.png" alt="Q2 Pod: the iPod firmware mod for the Shanling Q2" width="100%">
  </a>
</p>

<p align="center">
  An iPod classic style firmware mod for the Shanling Q2.<br>
  Wheel navigation, parametric EQ and Coverflow, built on the stock firmware.
</p>

<p align="center">
  <a href="https://github.com/DiamondBond/q2-pod/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/DiamondBond/q2-pod?style=flat-square&label=release&color=3D424B"></a>
  <a href="#features"><img alt="Device: Shanling Q2" src="https://img.shields.io/badge/device-Shanling%20Q2-B99AC8?style=flat-square"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-D77868?style=flat-square"></a>
  <a href="https://youtu.be/C5x05EwsPGI"><img alt="Watch on YouTube" src="https://img.shields.io/badge/YouTube-video-FF0000?style=flat-square&logo=youtube&logoColor=white"></a>
  <a href="https://discord.gg/pgsQpXhSQ"><img alt="Join the Discord" src="https://img.shields.io/badge/Discord-join-5865F2?style=flat-square&logo=discord&logoColor=white"></a>
</p>

<p align="center">
  <b><a href="https://github.com/DiamondBond/q2-pod/releases/latest">Download</a></b> ·
  <b><a href="#install">Install</a></b> ·
  <b><a href="docs/changelog.md">Changelog</a></b> ·
  <b><a href="#documentation">Docs</a></b>
  <br>
  <a href="#display-settings">Display</a> |
  <a href="#ipod-ui">iPod UI</a> |
  <a href="#controls">Controls</a> |
  <a href="#parametric-eq">Parametric EQ</a> |
  <a href="#microsd-card">microSD card</a> |
  <a href="#coverflow">Coverflow</a>
</p>

## Features

<table>
<tr>
<td width="50%" valign="top">

**Feel**

- iPod classic lists, Home and Now Playing
- Wheel with acceleration and position memory
- Coverflow and a Now Playing visualizer
- Accent, Home layout and battery style
- Charge limit (80%) and Low power mode

</td>
<td width="50%" valign="top">

**Music**

- Hold Centre: Favourites, Add to playlist, Shuffle, Go to album/artist
- Shuffle Songs and Most Played (your top 100)
- Parametric EQ: up to 30 bands, per channel, with balance and a live curve
- Sorting ignores a leading The, A or An

</td>
</tr>
<tr>
<td valign="top">

**Beyond music**

- Videos, Photos and Books (`.txt`, `.epub`)
- Podcasts and Audiobooks that resume where you left off
- Long mixes resume too

</td>
<td valign="top">

**Scrobbling**

- Every listen logged to `.scrobbler.log`
- Upload to Last.fm or ListenBrainz over Wi-Fi

</td>
</tr>
<tr>
<td colspan="2" valign="top">

**Fixed under the hood**: no choppy AirPods audio (AAC) · clock survives power-off · less battery drain with the screen off · faster library browsing · long VBR MP3s start at once and seek accurately

</td>
</tr>
</table>

## Install

> [!IMPORTANT]
> Charge the Q2 first, and leave the microSD card in until the update finishes.

1. [Download](https://github.com/DiamondBond/q2-pod/releases/latest) `Q2.Firmware.V*.zip`, unzip it and copy `update.tar` to the root of the microSD card.
2. On the Q2: **System settings → System Update → TF card update**.
3. **System settings → About** then shows FW V1.32 and your **CFW. Version**.

**Prefer the stock look?** `Q2.Firmware.V*-stock.zip` has everything except the [iPod UI](#ipod-ui).

### Rockbox

[Rockbox](https://github.com/DiamondBond/q2-rockbox) can run next to Q2 Pod from the microSD card: unzip its [`rockbox.zip`](https://github.com/DiamondBond/q2-rockbox/releases) to the card's root and the Q2 starts Rockbox at power-on. **Hold Play/Pause while powering on**, or Rockbox's **Boot stock OS**, to start Q2 Pod for that session; the next power-on is Rockbox again. Ported themes: [q2-rockbox-themes](https://github.com/DiamondBond/q2-rockbox-themes/releases). See [Boot](docs/boot.md#rockbox).

### Restore stock

Flash the [official firmware](https://en.shanling.com/download/150) the same way. If the UI won't start, copy the `recovery-update` folder from [Shanling's recovery package](https://drive.google.com/file/d/1aINQfJu6n0JTQ4hOzzD1uSpSj3TS_NJj/view?usp=drive_link) to the card, then hold previous-song and power on with the centre button.

## Display settings

**System settings → Display**:

| Setting     | Options                                                   |
| ----------- | --------------------------------------------------------- |
| **Accent**  | Graphite (default), Crimson (stock red), Tidal, Champagne |
| **Home**    | Split (list beside the cover) or Full (list only)         |
| **Battery** | Icon (default), Percent, Icon + Percent                   |

## Battery and library settings

| Where                                  | Setting          | What it does                                                                                                                            |
| -------------------------------------- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| **System settings → Power management** | **Charge limit** | **80%** stops charging at 80% and starts again at 75%, so a Q2 left plugged in isn't held full. Off by default.                         |
| **System settings → Power management** | **Low power**    | Longer battery: the second CPU core sleeps while the screen is off, and the screen-on UI idles when you don't touch it. Off by default. |
| **System settings → Power management** | **Wake**         | **Double click** (default) or **Single click**: Centre wakes the screen and unlocks the player. |
| **Audio settings**                     | **Artists**      | **Artist** (default) or **Album Artist**: browse Artists by the Album Artist tag, so guest artists don't split albums.                  |

Charge limit applies while the Q2 is on; charging while it's powered off is stock's. Low power never touches the sound, EQ, brightness or radios. Audio settings also has stock's DAC **Filter**.

## iPod UI

- **Home:** a list (Now Playing, Library, Coverflow…) beside the playing track's cover, instead of the carousel.
- **Lists:** four rows per screen, full-width accent bar; **`>`** marks rows that open another list.
- **Status bar:** play state, EQ, time, Bluetooth, Wi-Fi, battery. The Bluetooth codec (AAC, LDAC…) shows briefly on connect.
- **Now Playing:** "3 of 12", large rounded cover beside title, artist and album, and a slim accent progress capsule.
- **Visualizer:** swipe Now Playing to its fourth page: Spectrum, Oscilloscope, VU Meters or Halo. Tap to switch; your choice is kept. It follows what you hear, EQ included.
- **Quick settings:** pull down from the top edge.
- **Slider settings:** turn the wheel to adjust brightness, maximum volume, startup volume or balance. In Quick Settings, the wheel adjusts brightness.
- **Shutdown:** System settings → Power management → Shut down, then confirm. Holding Centre never shuts down.
- **Wake:** double-click Centre by default. System settings → Power management → Wake can switch to a single click, including unlocking.
- **Page slides:** in from the right, back out on Return.
- **Fast-scroll letter:** spinning a long list shows the letter it sorts under (C for The Cure).
- **Starts on Home.** **Memory playback → Location** restores your queue, paused; **Track** restarts the song. **In-Vehicle mode** starts playing.

## Controls

| Control                   | What it does                                                                                                                                                     |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Wheel**                 | One row per tick; spin to speed up. On Now Playing it controls volume, seeking or playback modes, depending on the bottom control. |
| **Centre button**         | Opens the highlighted item. On Now Playing, cycles Progress → Seek → Playback mode. Double-click turns the screen off. |
| **Double Centre**         | Turns the screen off. On the lock screen, unlocks the player.                                                                                                   |
| **Hold Centre**           | Song: queue, favourite, playlist, go to album or artist. Album, artist, genre or folder: queue, shuffle, playlist; a Coverflow album: queue, shuffle, go to artist. |
| **Hold Play/Pause**       | Opens Now Playing without changing playback.                                                                                                                    |

On Now Playing, Seek adds a position thumb. Turn the wheel to move five seconds per tick, or drag the bar directly. After wheel seeking, Centre returns to Progress; without a change, it advances to Playback mode. Playback mode replaces the bar with the current mode; turn the wheel to select List play, Repeat one, Shuffle songs or Repeat all. The normal progress view keeps the wheel on volume, or lyric scrolling while lyrics are shown.
| **Hold Return** (iPod)    | Opens Now Playing; the next Return goes back.                                                                                                                    |
| **Pull to search** (iPod) | At the top of Library (Local Songs), pull down until "Release to search" appears.                                                                                |

- **List ends:** lists stop at the end; pause, then turn again to wrap.
- **Position memory:** recent folders, albums, searches and menus reopen where you were, until power-off.
- **Artists:** open on Albums, with All Songs one tap away. Browse by the Album Artist tag with **Audio settings → Artists**.
- **Pop-ups** (iPod): the wheel moves between OK and Cancel.
- **Key Tone:** the speaker clicks only when nothing plays and no headphones (3.5/4.4 mm), Bluetooth or USB DAC are connected. iPod: once per row, not per tick.

### Photos, Books and Videos

| Screen    | Wheel                  | Centre                                | Other                                                       |
| --------- | ---------------------- | ------------------------------------- | ----------------------------------------------------------- |
| **Photo** | Previous or next photo | Shows or hides "3 of 40" and the name | **Return** goes back                                        |
| **Book**  | Turns the pages        | Shows or hides progress               | **Return** goes back; books reopen where you left off       |
| **Video** | Volume                 | Toggles seek: 10 s per tick           | **Play/Pause** pauses, previous/next skip, **Return** exits |

Video sound plays on the headphone jack, Bluetooth or a USB DAC. Music stops meanwhile. Decoding is software, so encodes near the screen's 375 × 320 play smoothest.

## Parametric EQ

**Audio settings → Equalizer**:

- **Curve:** the response is drawn above the list as you edit, with a ring on each band.
- **Bands:** Peaking, Low shelf or High shelf; frequency, gain, Q, on/off, channel (both, **L** or **R**).
- **Gain:** -24 to +24 dB on the wheel; picking one turns the band on, so the ten default bands (31 Hz to 16 kHz, Q 1.41) work as a graphic EQ.
- **Frequency and Q:** tap the value (or press centre) to type one, or step with **Raise** / **Lower**.
- **Balance:** L 12.0 dB to R 12.0 dB in 0.5 dB steps; **R 1.0 dB** plays the left 1 dB quieter.
- **Apply changes:** edits and presets take effect only when chosen.
- **PEQ: ON/OFF:** applies at once and persists; the status-bar **EQ** icon follows it.
- **Preamp:** **Auto** cuts just enough that boosts don't clip. Or pick +12 to -24 dB; above Auto, loud boosts can clip.

### Import a preset

1. Copy an AutoEQ / Equalizer APO `.txt` to `/EQ/` on the card. `Channel: L`, `R` and `all` sections work.
2. **Presets → Import from SD /EQ**, then pick the file.
3. Select the saved preset, then **Apply changes** (and **PEQ: ON**).

## microSD card

Media folders go at the card's root, any capitalisation; each adds its **Library** row when present. Caches are safe to delete and rebuild as needed (Coverflow and Photos need 16 MB free).

| Path                       | What it is                                                                              | Made by |
| -------------------------- | --------------------------------------------------------------------------------------- | ------- |
| `Podcasts/`, `Audiobooks/` | One folder per show or book; always resume, never count as plays                        | You     |
| `Photos/`                  | `.jpg`, `.jpeg`, `.png` (JPEG up to 6 MB, PNG 1 MB); subfolders are albums              | You     |
| `Books/`                   | `.txt`, `.epub` (no DRM); one level of subfolders                                       | You     |
| `Videos/`                  | `.mp4`, `.m4v`, `.mkv`, `.avi`, `.mov`, `.mpg`; one level of subfolders                 | You     |
| `EQ/`                      | AutoEQ / Equalizer APO presets to [import](#import-a-preset)                            | You     |
| `.scrobble.ini`            | Scrobble accounts (sample below); adds **Upload Scrobbles**                             | You     |
| `.scrobble.pem`            | Optional CA bundle (e.g. [curl's](https://curl.se/ca/cacert.pem)) so uploads verify TLS | You     |
| `.scrobbler.log`           | Every listen, Rockbox format; for Upload Scrobbles or any uploader                      | Q2 Pod  |
| `.scrobbler.log.sent`      | Listens already uploaded                                                                | Q2 Pod  |
| `.coverflow/`              | Coverflow artwork cache                                                                 | Q2 Pod  |
| `.photos/`                 | Photo thumbnails and screen-size copies                                                 | Q2 Pod  |
| `.books/`                  | EPUBs converted to text                                                                 | Q2 Pod  |
| `.sldp/`                   | Stock's own cover cache                                                                 | Stock   |

`.scrobble.ini` takes a ListenBrainz token ([your settings](https://listenbrainz.org/settings/)), a Last.fm account, or both:

```ini
[LISTENBRAINZ]
TOKEN=your-listenbrainz-user-token

[LASTFM]
USER=your-username
PASSWORD=your-password
API_KEY=your-api-key
API_SECRET=your-shared-secret
```

Get Last.fm's key and secret from your own [API account](https://www.last.fm/api/account/create) (any name works). The file is plain text, so keep the card to yourself.

**Upload Scrobbles** is the second-last row of **Library**, just above Update Local Music, used while on Wi-Fi. It only shows when the file is named exactly `.scrobble.ini` (leading dot, no hidden `.txt`) and saved as plain text. Leave out any section you don't use: a placeholder token still counts as an account, and its failures stop every upload.

Settings, play counts, resume points and book pages live on the Q2 itself (`/mnt/data`), not the card.

## Coverflow

- Run **Update Local Music** first.
- **First open** prepares artwork once; **Cancel** keeps progress. Later opens add only new albums; **Refresh library**, the last card, rebuilds everything.
- **Sort**, the card before it: press to switch between **Album**, **Artist** (then year), **Recently Added** and **Most Played**. The choice is kept.
- **Artwork:** `cover.jpg`, `folder.jpg`, then embedded art; otherwise a placeholder.

## Documentation

| Document                       | Covers                                             |
| ------------------------------ | -------------------------------------------------- |
| [Changelog](docs/changelog.md) | Every release                                      |
| [iPod UI](docs/ipod.md)        | Layout audit and iPod features                     |
| [Internals](docs/internals.md) | Hooks, selection, position memory, timing, drawing |
| [Building](docs/building.md)   | Building both variants and the MIPS test suite     |
| [Releasing](docs/releasing.md) | Packaging, verifying and publishing                |
| [Boot](docs/boot.md)           | Boot sequence, Rockbox, framebuffer and the splash |
| [Setup](docs/setup.md)         | Q2 Pod, Rockbox and themes on a fresh Q2           |

## Contributing

Found a bug? [Open an issue](https://github.com/DiamondBond/q2-pod/issues) with the screen and steps. To build it yourself, see [Building](docs/building.md).

## License

[MIT](LICENSE) for this repository's code and docs. Shanling's Q2 firmware, which the release images are built from, remains Shanling's. Not affiliated with Shanling.
