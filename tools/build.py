#!/usr/bin/env python3
"""Reproducibly patch only the audited Q2 V1.32 ZIP. Requires LLVM and squashfs-tools, and ImageMagick for --ipod.

--logo swaps the boot splash JPEG (320x375); it defaults to assets/boot-logo.jpg.
"""
import argparse, fcntl, hashlib, io, json, pathlib, re, shlex, struct, subprocess, sys, tarfile, zipfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
ZIP_SHA = '154c17822d09be001be35c03d2d3488424dee195221790bd70864480d55b0f00'
DEMO_SHA = '2c5f06142850b4fc168f82b44a81550cce0a5b4b9fe1c179dced4a08a3049138'
VERSION = '8.7'
# The updater's identity (firmware_v20.info and demo's version literal), 5 characters; About shows
# the stock firmware version and a CFW. Version row with the edition instead (ringnav_about).
def base36(number):
    digits = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    result = ''
    while number:
        number, digit = divmod(number, 36)
        result = digits[digit] + result
    return result or '0'

def version_tag(release, variant, build_number=None):
    """Keep updater identities five characters long and retain the required V prefix."""
    edition = 'I' if variant == 'ipod' else 'S'
    if build_number is not None:
        if not 1 <= build_number < 36**3:
            raise ValueError('Development build number must be between 1 and 46655')
        return 'V' + base36(build_number).zfill(3) + edition.lower()
    tag = f'V{release}{edition}'
    if len(tag) == 5:
        return tag
    major, minor = map(int, release.split('.'))
    if not (0 <= minor < 100 and 0 <= major * 100 + minor < 36**3):
        raise ValueError('Release version exceeds the five-character updater tag capacity')
    return 'V' + base36(major * 100 + minor).zfill(3) + edition

VERSIONS = {variant: version_tag(VERSION, variant) for variant in ('stock', 'ipod')}
BASE = 0xb00000
SCRATCH = 0xb40000
RING_STEP = 48
HOOKS = {
    'on_wm_keyup_before_fun': (0x4e85c8, 'ringnav'),
    'on_wm_tsdown_before_fun': (0x4e8bd0, 'ringnav_touch'),
    'widget_on_paint_border': (0x6596a0, 'ringnav_paint'),
    'widget_dispatch': (0x65e0ec, 'ringnav_dispatch'),
    'on_wm_keylong_fun': (0x4e873c, 'ringnav_keylong'),
    'playset_equalizer_page_init': (0x4b642c, 'peq_page_init'),
    'set_equalizer_value': (0x4f9230, 'peq_stock_eq'),
    'home_page_init': (0x523c84, 'coverflow_home'),
    'localmusic_page_init': (0x524424, 'ringnav_localmusic'),
    # Library lists: an Unknown row with no songs is dropped
    'load_localclass_list': (0x5088cc, 'ringnav_localclass'),
    # The three songtable writers; Coverflow keeps its album list until one runs.
    'scanAllMusicFile': (0x4fc788, 'coverflow_scan_all'),
    'scanSpecFolder': (0x4fc964, 'coverflow_scan_folder'),
    'deleteMusicFromMusicDb': (0x500b5c, 'coverflow_delete_song'),
    'main_loop_sleep_default': (0x648f00, 'ringnav_sleep'),
    'systemset_about_page_init': (0x4bc80c, 'ringnav_about'),
    # Podcasts and Audiobooks: folder_page opened at their folder, and Back there leaving it
    'folder_page_init': (0x523330, 'ringnav_folder'),
    'folder_back': (0x507ac8, 'ringnav_folder_back'),
    # Videos: no input reaches the UI while q2video plays
    'window_manager_dispatch_input_event': (0x66d49c, 'ringnav_input'),
    # Key Tone: no click while music plays, and none on the buzzer while headphones or Bluetooth listen
    'buzzeer_switch': (0x4f3cc8, 'ringnav_buzzer'),
    # Power management gains Charge limit and Low power; Audio settings gains Artists (Album Artist)
    'systemset_powermanager_page_init': (0x4c72d0, 'ringnav_powermanager'),
    'playset_playset_page_init': (0x4b98d8, 'ringnav_audioset'),
}
# Hooked in iPod builds only, so Stock keeps these entry points stock.
IPOD_HOOKS = {'widget_on_paint_background': (0x65c77c, 'ringnav_paint_bg'),
              'playing_page_init': (0x52ca88, 'ringnav_playing'),
              'systemset_display_page_init': (0x4c1d04, 'ringnav_display'),
              'dialog_confirminfo_dialog_init': (0x4989ec, 'ringnav_confirm_dialog'),
              'style_get_color': (0x649f6c, 'ringnav_style_color'),
              'image_manager_add': (0x6445d4, 'ringnav_image_add'),
              'on_wm_keydown_before_fun': (0x4e8424, 'ringnav_keydown')}
# The payload's stock_<name>_trampoline resumes each hook past its 3-word PIC prologue, in this order.
TRAMPOLINES = {'keyup': 'on_wm_keyup_before_fun', 'touch': 'on_wm_tsdown_before_fun', 'paint': 'widget_on_paint_border',
               'dispatch': 'widget_dispatch', 'keylong': 'on_wm_keylong_fun', 'eq': 'set_equalizer_value',
               'home': 'home_page_init', 'localmusic': 'localmusic_page_init', 'localclass': 'load_localclass_list',
               'paint_bg': 'widget_on_paint_background', 'playing': 'playing_page_init',
               'display': 'systemset_display_page_init', 'color': 'style_get_color', 'image': 'image_manager_add',
               'confirm_dialog': 'dialog_confirminfo_dialog_init',
               'keydown': 'on_wm_keydown_before_fun', 'scan_all': 'scanAllMusicFile', 'scan_folder': 'scanSpecFolder',
               'delete_song': 'deleteMusicFromMusicDb', 'sleep': 'main_loop_sleep_default',
               'about': 'systemset_about_page_init', 'folder': 'folder_page_init', 'folder_back': 'folder_back',
               'input': 'window_manager_dispatch_input_event', 'buzzer': 'buzzeer_switch',
               'power': 'systemset_powermanager_page_init', 'audioset': 'playset_playset_page_init'}
# Every audited stock PIC prologue resolves this GOT base.
GP = 0xa26cc0
# iPod: style_get_gradient has no PIC prologue. It is a leaf that null-checks the style and its
# vtable, then tail-calls get_gradient (+0x18); its first two words (beqz a0; nop) become the jump
# and the payload does the whole of it. The third word is pinned too, so the layout is the audited one.
IPOD_LEAF = ('style_get_gradient', 0x649f3c, 'ringnav_style_gradient', (0x10800009, 0, 0x8c820000))
# Videos: window_manager_paint is a leaf too (null-checks the manager and its vtable at +0x94, then
# tail-calls paint, +0xc); the payload does the whole of it and skips it while q2video plays.
WM_PAINT_LEAF = ('window_manager_paint', 0x66d46c, 'ringnav_wm_paint', (0x10800009, 0, 0x8c820094))
# Videos' player (patch/video.c): a separate executable against the rootfs's own libraries,
# with display_logo's inode metadata.
HELPER, HELPER_LIKE = 'usr/bin/q2video', 'usr/bin/display_logo'
HELPER_LIBS = ['lib/libc-2.28.so', 'lib/libpthread-2.28.so', 'usr/lib/libasound.so.2.0.0']
# Rockbox dual boot (docs/boot.md#rockbox): S90play starts Rockbox whenever the card has it, as an
# iPod with Rockbox does. Play/Pause held at power-on (patch/boot.c reads the key) or Rockbox's
# exit 0x51 (Boot stock OS) starts Q2 Pod for that session only; nothing is saved, so the next
# power-on tries Rockbox again. ponytail: the card's mount is awaited (up to 3 s without a card)
# unless Q2 Pod was asked for, as the card probe can't tell "no card" from "not yet". Rockbox runs
# with the launcher contract in its tools/shanlingq2/README; demo starts when it exits, or when the
# card has none. exec keeps demo's argv[0], which checkappprocess.sh pgreps for.
BOOT = 'usr/bin/q2boot'
S90PLAY = 'etc/init.d/S90play'
S90PLAY_SHA = 'a6a7ed7d9a10e38801f4a41ec6f3c0ce2bc07c00d213c9278785c5f8d4520e24'
BOOT_HOOK = (b'    /release/bin/demo &\n', b'''    (
        if ! /usr/bin/q2boot; then
            for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
                [ -e /tmp/mmc_add ] && break
                usleep 200000
            done
            rb=/mnt/mmc/.rockbox
            if [ -f $rb/rockbox ]; then
                (cd $rb && exec ./rockbox) > $rb/rockbox.log 2>&1
                echo "exit $?" >> $rb/rockbox.log
            fi
        fi
        exec /release/bin/demo
    ) &
''')
# The byte in bluealsa's AAC capability holding the 44.1 kHz bit; see docs/internals.md.
BLUEALSA = 'usr/bin/bluealsa'
BLUEALSA_SHA = '0a4ffb7cc8207a46a3568440c5f31022b7125befd164e2f1af52537340a9892a'
AAC_44K1 = 0x317b8
# platform_init's crash watchdog forks pgrep every 2 s; 10 s is still quick to reboot a dead UI.
WATCHDOG = 'usr/bin/checkappprocess.sh'
WATCHDOG_SHA = '68843ed739919420974ca55e6c5a6a2e52e63711b2faca116656440e5bd6a096'
WATCHDOG_SLEEP = (b'\tsleep 2\n', b'\tsleep 10\n')
# toolsSetRtcTime's command, with its padding; -u keeps the RTC in UTC. See docs/internals.md#clock.
RTC_WRITE = (b'hwclock -w\0\0', b'hwclock -wu\0')
# mclNextSong's shuffle pick; the payload calls the stock pick, then applies a pending Play next.
SHUFFLE_CALL = (0x5addf0, 0x0411e8cb)  # bal mcl_shuffle_pick; its delay slot (a0=1) stays
# The bal toolsTrimLeft on each name copy in the two library name comparators (0x5b9d40, 0x5ba658, the
# Chinese and other-language sorts); they become jal ringnav_sort_key, which also drops a leading article.
SORT_TRIMS = (0x5b9e38, 0x5b9ea4, 0x5ba750, 0x5ba7bc)
# check_mem_thd's "open failed, skip the write" beq becomes b: it never writes 3 to drop_caches.
DROP_CACHES = (0x5120a8, 0x12220006, 0x10000006)
# The knob's travel per tick: get_direction's threshold (knob units, 200 a turn) in the rotation
# handler, stock 20 for a gesture's first tick (and a release without one) and 10 after, scaled by
# WHEEL_TRAVEL. At stock a small nudge moved a row. Tuned on the device.
WHEEL_TRAVEL = 1.2
WHEEL_THRESHOLDS = ((0x625ce0, 0x24060014), (0x625cdc, 0x2403000a), (0x625ba0, 0x24060014))
# Now Playing More's first row and the page it opens are the play queue, which stock English calls
# "Playlists" (player_playlist, used nowhere else; Local Music's are "Playlist"). Same size: the
# table finds each value by offset, so the padding NULs are never read.
QUEUE_LABEL = (b'player_playlist\0Playlists\0', b'player_playlist\0Queue\0\0\0\0\0')
# demo's .pdr section (offset, size): MIPS procedure descriptors past every LOAD segment, which nothing
# reads at run time. Zeroed, they pack to almost nothing, which keeps the Stock rootfs within stock's size.
PDR = (0x61694c, 0x69d40)

def run(*args):
    return subprocess.check_output([str(a) for a in args], text=True)
def sha(b): return hashlib.sha256(b).hexdigest()

def source_sha256():
    """Hash every build input, so a test run cannot silently use a stale output directory."""
    h = hashlib.sha256()
    tools = [ROOT/'tools'/f for f in ('build.py', 'ipod.py', 'peq.py', 'release.py')]
    for path in sorted([*(p for p in ROOT.glob('assets/**/*') if p.is_file()), *ROOT.glob('patch/*'), *tools]):
        h.update(str(path.relative_to(ROOT)).encode() + b'\0')
        h.update(path.read_bytes())
    return h.hexdigest()

def check(condition, message):
    if not condition: raise ValueError(message)
def jpeg_size(b):
    """Validate the splash's supported JPEG frame header and return its dimensions."""
    check(b[:2] == b'\xff\xd8', 'Logo must be a JPEG')
    i = 2
    while i < len(b):
        check(b[i] == 0xFF, 'Malformed JPEG')
        while i < len(b) and b[i] == 0xFF: i += 1
        check(i < len(b), 'Truncated JPEG marker')
        marker = b[i]
        i += 1
        if marker in (0xD9, 0xDA): break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7: continue
        check(marker not in (0, 0xD8), 'Malformed JPEG marker')
        check(i + 2 <= len(b), 'Truncated JPEG segment')
        size = struct.unpack_from('>H', b, i)[0]
        check(size >= 2 and i + size <= len(b), 'Invalid JPEG segment length')
        if marker in (0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF):
            check(size >= 8 and size == 8 + 3 * b[i+7], 'Invalid JPEG frame length')
            # Stock display_logo indexes decoded pixels as RGB triples (0x400ec0 onward),
            # but leaves libjpeg's output color space unchanged. Grayscale/CMYK are unsafe.
            check(marker == 0xC0 and b[i+2] == 8 and b[i+7] == 3,
                  'Logo must be an 8-bit, three-component baseline JPEG (not grayscale/CMYK)')
            h, w = struct.unpack_from('>HH', b, i+3)
            check(w > 0 and h > 0, 'Invalid JPEG dimensions')
            return w, h
        i += size
    raise ValueError('No JPEG size marker')
def symbols(p, table=None):
    out = {}
    for line in (table or run('readelf', '-Ws', p)).splitlines():
        s = line.split()
        if len(s) >= 8 and s[0].endswith(':'):
            try: out[s[7]] = int(s[1], 16)
            except ValueError: pass
    return out

def segments(b):
    phoff = struct.unpack_from('<I', b, 28)[0]
    size, num = struct.unpack_from('<HH', b, 42)
    check(size == 32, 'Unexpected ELF program header size')
    return [(phoff+i*size, struct.unpack_from('<8I', b, phoff+i*size)) for i in range(num)]
def fileoff(b, a):
    for _, (t, o, v, _, f, _, _, _) in segments(b):
        if t == 1 and v <= a < v+f: return o+a-v
    raise ValueError(f'Unmapped address {a:x}')

FUNCTIONS = {
 'widget_on': ('unsigned', 'void *, unsigned, int (*)(void *, void *), void *'),
 'widget_set_visible': ('int', 'void *, int, int'),
 'widget_set_opacity': ('int', 'void *, unsigned'),
 'widget_set_enable': ('int', 'void *, int'),
 'widget_set_text_utf8': ('int', 'void *, const char *'),
 'widget_set_text': ('int', 'void *, const unsigned *'),
 'locale_info': ('void *', 'void'),
 'locale_info_tr': ('const char *', 'void *, const char *'),
 'widget_use_style': ('int', 'void *, const char *'),
 'widget_set_name': ('int', 'void *, const char *'),
 'widget_set_sensitive': ('int', 'void *, int'),
 'widget_to_local': ('int', 'void *, void *'),
 'widget_dispatch_event_to_target_recursive': ('int', 'void *, void *'),
 'label_create': ('void *', 'void *, int, int, int, int'),
 'stock_search': ('int', 'void *, void *'),
 'widget_set_children_layout': ('int', 'void *, const char *'),
 'widget_resize': ('int', 'void *, int, int'),
 'widget_lookup': ('void *', 'void *, const char *, int'),
 'scroll_bar_scroll_to': ('int', 'void *, int, unsigned'),
 'navigator_back_to_home': ('int', 'void'),
 'navigator_switch_to_with_context': ('int', 'const char *, const void *, int'),
 'window_manager': ('void *', 'void'),
 'window_manager_get_top_window': ('void *', 'void *'),
 'window_manager_is_animating': ('int', 'void *'),
 'window_manager_get_pointer_pressed': ('int', 'void *'),
 'widget_get_visible': ('int', 'void *'),
 'widget_get_prop_bool': ('int', 'void *, const char *, int'),
 'widget_get_prop_int': ('int', 'void *, const char *, int'),
 'widget_get_prop_str': ('const char *', 'void *, const char *, const char *'),
 'widget_get_text': ('const unsigned *', 'void *'),
 'widget_get_type': ('const char *', 'void *'),
 'widget_count_children': ('unsigned', 'void *'),
 'widget_get_child': ('void *', 'void *, unsigned'),
 'widget_set_prop_int': ('int', 'void *, const char *, int'),
 'widget_set_prop_str': ('int', 'void *, const char *, const char *'),
 'pages_set_active_by_name': ('int', 'void *, const char *'),
 'widget_invalidate_force': ('int', 'void *, void *'),
 'widget_animator_start': ('int', 'void *'),
 'widget_animator_scroll_set_params': ('int', 'void *, int, int, int, int'),
 'slide_menu_set_value': ('int', 'void *, int'),
 'slide_menu_item_width': ('int', 'void *'),
 'slide_menu_on_scroll_done': ('int', 'void *, void *'),
 'widget_animator_scroll_create': ('void *', 'void *, unsigned, unsigned, int'),
 'widget_animator_on': ('unsigned', 'void *, unsigned, int (*)(void *, void *), void *'),
 'widget_set_focused': ('int', 'void *, int'),
 'widget_animator_pause': ('int', 'void *'),
 'widget_animator_destroy': ('int', 'void *'),
 'canvas_get_clip_rect': ('int', 'void *, void *'),
 'canvas_set_clip_rect': ('int', 'void *, const void *'),
 'canvas_set_fill_color': ('int', 'void *, unsigned'),
 'canvas_set_stroke_color': ('int', 'void *, unsigned'),
 'canvas_stroke_rect': ('int', 'void *, int, int, int, int'),
 'canvas_fill_rect': ('int', 'void *, int, int, int, int'),
 'canvas_draw_icon': ('int', 'void *, void *, int, int'),
 'canvas_set_font': ('int', 'void *, const char *, unsigned'),
 'canvas_set_text_color': ('int', 'void *, unsigned'),
 'canvas_draw_text_in_rect': ('int', 'void *, const unsigned *, unsigned, const void *'),
 'canvas_draw_text': ('int', 'void *, const unsigned *, unsigned, int, int'),  # Books (books.c)
 'canvas_measure_text': ('float', 'void *, const unsigned *, unsigned'),
 'canvas_fill_rounded_rect': ('int', 'void *, const void *, const void *, const void *, unsigned'),
 'canvas_stroke_rounded_rect': ('int', 'void *, const void *, const void *, const void *, unsigned, unsigned'),
 # The PEQ curve (peq_ui.c) and the visualizer (visualizer.c): AWTK's software nanovg, float_t float, color_t by value
 'canvas_get_vgcanvas': ('void *', 'void *'),
 'vgcanvas_save': ('int', 'void *'), 'vgcanvas_restore': ('int', 'void *'),
 'vgcanvas_translate': ('int', 'void *, float, float'),
 'vgcanvas_begin_path': ('int', 'void *'), 'vgcanvas_close_path': ('int', 'void *'),
 'vgcanvas_move_to': ('int', 'void *, float, float'), 'vgcanvas_line_to': ('int', 'void *, float, float'),
 'vgcanvas_arc': ('int', 'void *, float, float, float, float, float, int'),
 'vgcanvas_fill': ('int', 'void *'), 'vgcanvas_stroke': ('int', 'void *'),
 'vgcanvas_set_fill_color': ('int', 'void *, unsigned'), 'vgcanvas_set_stroke_color': ('int', 'void *, unsigned'),
 'vgcanvas_set_fill_linear_gradient': ('int', 'void *, float, float, float, float, unsigned, unsigned'),
 'vgcanvas_set_line_width': ('int', 'void *, float'), 'vgcanvas_set_line_cap': ('int', 'void *, const char *'),
 'pointer_event_init': ('void *', 'void *, int, void *, int, int'),
 'time_now_ms': ('unsigned', 'void'),
 'sleep_ms': ('int', 'unsigned'),
 'timer_add': ('unsigned', 'int (*)(const void *), void *, unsigned'),
 'timer_remove': ('int', 'unsigned'),
 'tk_strcmp': ('int', 'const char *, const char *'),
 'slide_menu_scroll_to_next': ('int', 'void *'),
 'slide_menu_scroll_to_prev': ('int', 'void *'),
 'table_client_stop_animator_scroll': ('int', 'void *'),
 'table_client_set_yoffset': ('int', 'void *, int'),
 'widget_dispatch_simple_event': ('int', 'void *, unsigned'),
 'scroll_view_set_offset': ('int', 'void *, int, int'),
 'table_client_scroll_to': ('int', 'void *, int'),
 'scroll_view_scroll_delta_to': ('int', 'void *, int, int, int'),
 'widget_destroy_children': ('int', 'void *'),
 'list_item_create': ('void *', 'void *, int, int, int, int'),
 'hscroll_label_create': ('void *', 'void *, int, int, int, int'),
 'set_hscroll_label_attribute': ('void', 'void *'),
 'hscroll_label_set_only_focus': ('int', 'void *, int'),
 'hscroll_label_set_ellipses': ('int', 'void *, int'),
 'widget_off_by_func': ('int', 'void *, unsigned, void *, void *'),
 'window_close': ('int', 'void *'),
 'navigator_to': ('int', 'const char *'),
 'navigator_to_with_context': ('int', 'const char *, const void *'),
 'window_manager_get_input_device_status': ('char *', 'void *'),
 'airplayGetFlag': ('int', 'void'),
 'tk_snprintf': ('int', 'char *, unsigned, const char *, ...'),
 'toolsTimeItoa': ('int', 'char *, int'),
 'getMusicByAlbum': ('int', 'const char *'),
 'getMusicByAlbumAndSonger': ('int', 'const char *, const char *, int'),
 'getMusicByAlbumAndAlbumSonger': ('int', 'const char *, const char *, int'),
 'toolsLoadDirectory': ('int', 'const char *'),
 # Queue menu rows (navigation.c): artist/composer/genre queries, My Fav, the batch selection record
 'getMusicBySonger': ('int', 'const char *'),
 'getMusicByAlbumArtist': ('int', 'const char *'),
 'getMusicByComposer': ('int', 'const char *'),
 'getMusicByGenre': ('int', 'const char *'),
 'getMusicByAlbumAndComposer': ('int', 'const char *, const char *, int'),
 'getMusicByAlbumAndGenre': ('int', 'const char *, const char *, int'),
 'checkFavExist': ('int', 'void *'),
 'deleteMusicFromFav': ('int', 'void *'),
 'batch_init_selectrecord': ('int', 'int'),
 'batch_set_selectitem': ('int', 'int'),
 'batch_add_file': ('int', 'int, int, void *, void *, int'),
 'navigator_window_is_exist': ('int', 'const char *'),
 'mclLoadPlayList': ('int', 'void *, int, int'),
 'mcl_shuffle_pick': ('int', 'int'),
 'getAllAlbum': ('int', 'void'),
 # Shuffle Songs (navigation.c): every song, shuffle saved as the play-mode setting does, a random start
 'getAllMusic': ('int', 'int'),
 'config_playmode': ('int', 'int, int'),
 'toolsRandnum': ('int', 'int'),
 'widget_restack': ('int', 'void *, unsigned'),
 'toolsThumbSpecCover': ('int', 'const char *, const char *, int, int'),
 'toolsGetAlbumCover': ('int', 'const char *, const char *, int, int'),
 'window_create': ('void *', 'void *, int, int, int, int'),
 'widget_factory': ('void *', 'void'),
 'widget_factory_create_widget': ('void *', 'void *, const char *, void *, int, int, int, int'),
 'image_create': ('void *', 'void *, int, int, int, int'),
 'image_set_draw_type': ('int', 'void *, int'),
 'image_base_set_image': ('int', 'void *, const char *'),
 'widget_load_image': ('int', 'void *, const char *, void *'),
 'widget_unload_image': ('int', 'void *, void *'),
 'list_view_create': ('void *', 'void *, int, int, int, int'),
 'scroll_view_create': ('void *', 'void *, int, int, int, int'),
 'navigator_back': ('int', 'void'),
 'write_int_config': ('int', 'int, const char *, const char *'),
 'toolsReadConfig': ('int', 'const char *, const char *, const char *, char *, const char *'),
 'button_create': ('void *', 'void *, int, int, int, int'),
 'widget_move_resize': ('int', 'void *, int, int, int, int'),
 'tk_str_end_with': ('int', 'const char *, const char *'),
 'tk_str_start_with': ('int', 'const char *, const char *'),
 'image_manager': ('void *', 'void'),
 'image_manager_unload_all': ('int', 'void *'),
 'bitmap_get_line_length': ('unsigned', 'void *'),
 'bitmap_lock_buffer_for_write': ('unsigned char *', 'void *'),
 'bitmap_unlock_buffer': ('int', 'void *'),
 # Coverflow depth (coverflow.c): its frame bitmap, reading decoded covers, and the slide_menu stride
 'bitmap_create_ex': ('void *', 'unsigned, unsigned, unsigned, unsigned'),
 'bitmap_destroy': ('int', 'void *'),
 'bitmap_lock_buffer_for_read': ('const unsigned char *', 'void *'),
 'canvas_draw_image': ('int', 'void *, void *, const void *, const void *'),
 'slide_menu_set_spacer': ('int', 'void *, int'),
 'playing_timer_start': ('int', 'void *'),
 'playing_timer_clear': ('int', 'void *'),
 'player_seek_time': ('int', 'int'),
 'player_playtime_and_length': ('int', 'int *, int *'),  # elapsed, total: track seconds; -1 none
 'player_start': ('int', 'void *, int, int, int'),
 'buzzeer_switch': ('int', 'int'),  # the stock key click; it reads g_keytone_flag
 'on_wm_keyup_fun': ('int', 'void *, void *'),  # stock key-up: np_single replays a centre release
 'get_wifisignal': ('int', 'void'),  # the status bar's Wi-Fi bars, 1-4; -1 when not connected
 # Podcasts and Audiobooks (navigation.c): folder_page's reload of g_folder_path, title and list rebuild
 'folder_reload_data': ('int', 'void'),
 'folder_reinit_navbarname': ('int', 'void'),
 'folder_refresh': ('int', 'void *'),
 # Videos (books.c): stop the music, the DAC's power, and the screen and standby timeouts held off
 'mclGetOutputWay': ('int', 'void'),
 'mclGetBalance': ('int', 'void'),
 'mclGetLyricSize': ('int', 'void'),  # the playing track's lyric lines, 0 without
 'mclGetPlayStatus': ('int', 'void'),  # 1 stopped, 2 playing, 3 paused (mclStop, mclSetResume, mclSetPause)
 'player_stop': ('int', 'void'),
 'mclSetDacPwr': ('int', 'int'),
 'reset_poweroptions_timer': ('int', 'int, int, int'),
 'device_set_volume': ('int', 'int, int'),  # volume, notify: the DAC's or hciplayer's, as the volume dialog
 'toolsTrimLeft': ('void', 'char *'),
 'switch_charge_enable': ('int', 'int'),  # 1 charges: the BQ25890's /CE on GPIO PE22 (usbmode_page_init)
 # Coverflow's Sort: another ORDER BY over getAllAlbum's grouping, filled by its own row callback
 'toolsQueryDbTable': ('int', 'const char *, const char *, void *, int'),  # db, sql, row, name sort
 'album_row': ('int', 'void *, int, char **, char **'),
}
# Local stock routines in the SHA-256-pinned V1.32 executable.
PRIVATE_FUNCTIONS = {
    "stock_search": 0x5241c4,
    "slide_menu_item_width": 0x5f3040,
    "slide_menu_on_scroll_done": 0x5f3654,
    "mcl_shuffle_pick": 0x5a8120,
    "album_row": 0x4fc324,  # getAllAlbum's sqlite3_exec callback: id, album, songer, fileurl to a record
    "folder_refresh": 0x52176c,  # folder_page's navbar and table from p_deque_showlist (its init, back)
}
GLOBALS = ['g_backlight_status', 'g_lockscreen_pageflag', 'g_testmode_flag',
           'g_guideflag', 'g_poweroff_state', 'g_usblink_status', 'bt__recv_pageflag',
           'g_power_longkey', 'g_ingore_bootkey_flag', 'g_equalizer_flag', 'g_navbar_status', 'g_playcover_type',
           'g_keytone_flag', 'g_folder_layer', 'g_delete_flag', 'g_volume', 'g_maxvolume',
           'g_po_status', 'g_bal_status',  # 3.5 mm and 4.4 mm jacks: 1 plugged (check_headset_status)
           'g_usbvol_mode',  # USB DAC volume: 0 fixed, else the volume (config_usbvolmode, device_set_volume)
           'g_usbdac_chargeflag']  # USB mode's charge choice, which switch_charge_enable gets there
GLOBALS += ['g_lightness', 'g_bootvolume', 'g_bootvol_flag']
# Audited stock browsing state, deque pointers, art locks, the status bar widget
# (system_bar_init stores it), the playing cover's track path and the playing track's tags as
# player_get_id3info parsed them; sizes are checked against the ELF.
CONTEXT_DATA = {'g_folder_path': 1024, 'g_class_type': 4,
                'g_local_classinfo_save': 912, 'g_artist_type': 4, 'album_modetype': 4,
                'p_deque_showlist': 4, 'tools_pdeq_directory': 4, 'mcl_pdeqplaylist': 4,
                'parse_cover_mutex': 24, 'g_playcover_mutex': 24, 'system_bar': 4, 'g_lastcover_url': 1024,
                'g_dacoff_time': 4, 'p_vector_select_record': 4,
                'g_play_id3_info': 2716,
                # Artists' source (PLAYSET ARTISTTYPE, the artist page's switch: 1 album artist);
                # the battery level (0-100) and the charger's state (1, 2 charging), get_battery_capacity's
                'pdeq_btshowlist': 4, 'artist_type': 4, 'g_power_capacity': 4, 'g_power_chargestate': 4}
# Windows the payload creates at runtime (window_create), so no rootfs asset names them.
PAYLOAD_WINDOWS = {'coverflow_page', 'photos_page', 'books_page', 'mostplayed_page',
                   'libraryhistory_page', 'librarytools_page', 'btremove_page'}
ICONS = ['menu_coverflow.png', 'menu_coverflowdown.png']
# The stock EQ preset page and the images only it and the stock equalizer page show: the PEQ
# editor clears that page's widgets on init and never binds the preset button, so none can load.
STOCK_EQ = ['release/assets/default/raw/ui/playset/preseteq_page.bin'] + [
    f'release/assets/default/raw/images/xx/{n}.png' for n in
    ['eq_bg', 'eq_off', 'eq_sidebg', 'eqbox'] + [f'eq_{p}{s}' for p in
    ('blues', 'classical', 'custon', 'dance', 'jazz', 'metal', 'pop', 'rock', 'scene') for s in ('', '_select')]]
# iPod: the Home carousel's card and arrow images; only the stock home_page.bin names them.
CAROUSEL = [f'release/assets/default/raw/images/xx/menu_{n}.png' for n in
    [*(c + s for c in ('playing', 'music', 'folder', 'stream', 'playset', 'sysset') for s in ('', 'down')), 'left', 'right']]

FLAGS = ['--target=mipsel-linux-gnu','-march=mips32r2','-mabi=32','-mfp64',
         '-mno-abicalls','-fno-pic','-G0','-ffreestanding','-fno-builtin',
         '-fno-stack-protector','-fno-unwind-tables','-fno-asynchronous-unwind-tables',
         '-Oz','-Wall','-Wextra','-Werror']

def hooks(ipod): return HOOKS | IPOD_HOOKS if ipod else HOOKS

def compile_payload(out, ipod=False):
    """Compile and link the payload."""
    from peq import compile_common
    extra = compile_common(out, out/'stock-demo', ipod=ipod)  # also writes the libc/libcstl imports navigation.c uses
    run('clang',*FLAGS,f'-DIPOD={int(ipod)}','-I',out,'-c',ROOT/'patch/navigation.c','-o',out/'navigation.o')
    asm = ['.set noreorder', '.text']
    for x, hook in TRAMPOLINES.items():
        asm += [f'.globl stock_{x}_trampoline', f'stock_{x}_trampoline:', f'lui $gp, {GP >> 16}',
                f'ori $gp, $gp, {GP & 65535}', f'j 0x{(HOOKS | IPOD_HOOKS)[hook][0] + 12:x}', 'nop']
    (out/'trampoline.S').write_text('\n'.join(asm)+'\n')
    run('clang',*FLAGS,'-c',out/'trampoline.S','-o',out/'trampoline.o')
    run('ld.lld','-m','elf32ltsmip','--gc-sections','-T',ROOT/'patch/link.ld','-e','ringnav',
        *[f'--undefined={name}' for _, name in hooks(ipod).values()], *[f'--undefined={IPOD_LEAF[2]}'] * ipod,
        f'--undefined={WM_PAINT_LEAF[2]}',
        out/'navigation.o',out/'trampoline.o',*extra,'-o',out/'patch.elf')
    run('llvm-objcopy','-O','binary',out/'patch.elf',out/'patch.bin')
    return symbols(out/'patch.elf')

def compile_helper(out, cat, src, name, rels):
    """Link patch/src (and start.c) as name against the stock rootfs's libraries rels."""
    libs = []
    for rel in rels:
        libs.append(out/rel.rsplit('/', 1)[-1])
        libs[-1].write_bytes(cat(rel))
    objs = [out/f'{s}.o' for s in (src, 'start')]
    for o in objs:
        run('clang', *[f for f in FLAGS if f not in ('-mno-abicalls', '-G0')], '-mnan=2008', '-mabs=2008',
            '-mabicalls', '-c', ROOT/'patch'/f'{o.stem}.c', '-o', o)
    run('ld.lld', '-m', 'elf32ltsmip', '-e', '__start', '--dynamic-linker', '/lib/ld-linux-mipsn8.so.1',
        '--image-base=0x400000', '-z', 'noexecstack', '--gc-sections', '-s', '--hash-style=sysv', '--build-id=none',
        *objs, *libs, '-o', out/name)
    return (out/name).read_bytes()

def patch_s90play(raw):
    check(sha(raw) == S90PLAY_SHA, 'Unsupported S90play')
    return raw.replace(*BOOT_HOOK)

def append_payload(image, payload, base, memsz, label):
    """Map payload at base through the image's final PT_NULL header, R/W/X: payloads keep static state."""
    nulls = [(o,p) for o,p in segments(image) if p[0] == 0]
    check(len(nulls) == 1 and nulls[0][0] == segments(image)[-1][0], f'{label}: no final PT_NULL slot')
    check(all(p[2]+p[5] < base for _,p in segments(image) if p[0] == 1), f'{label}: payload mapping overlaps')
    off = (len(image)+65535)&~65535
    image.extend(bytes(off-len(image)))
    image.extend(payload)
    struct.pack_into('<8I',image,nulls[0][0],1,off,base,base,len(payload),memsz,7,65536)

def patch_bluealsa(raw):
    """Offer AAC at 48 kHz only. A headset that opens the stream itself (AirPods out of the case)
    picks 44.1 kHz, and bluealsa, still fed 48 kHz, drops about 8% of the AAC frames."""
    check(sha(raw) == BLUEALSA_SHA, 'Unsupported bluealsa binary')
    return raw[:AAC_44K1] + b'\0' + raw[AAC_44K1+1:]

def patch_watchdog(raw):
    check(sha(raw) == WATCHDOG_SHA, 'Unsupported watchdog script')
    return raw.replace(*WATCHDOG_SLEEP)

def build(zip_path, out, logo, ipod=False, dev=False, build_number=None):
    variant = 'ipod' if ipod else 'stock'
    check(dev == (build_number is not None), 'Development builds require a build number')
    version = version_tag(VERSION, variant, build_number)
    display_version = f'V{VERSION} {"iPod" if ipod else "Stock"}'
    if dev: display_version += f' dev {build_number}'
    out.mkdir(parents=True, exist_ok=True)
    check(not (out/'update.tar').exists(), 'Output already exists; use a fresh --out directory')
    source = source_sha256()
    raw = zip_path.read_bytes()
    check(sha(raw) == ZIP_SHA, 'Unsupported ZIP: SHA-256 differs from audited original')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        tarbytes = z.read('Q2 Firmware V1.32/update.tar')
    with tarfile.open(fileobj=io.BytesIO(tarbytes)) as t:
        meta = t.getmembers()
        check([m.name for m in meta] == ['firmware_v20.info','recovery-update',
              'recovery-update/xImage','recovery-update/rootfs.squashfs'], 'Unexpected package members')
        blobs = {m.name:t.extractfile(m).read() for m in meta if m.isfile()}
    info = blobs['firmware_v20.info'].decode().splitlines()
    check(info[:2] == ['Shanling Q2','V1.32'], 'Wrong model/version')
    for line in info[2:]:
        digest, name = line.split()
        check(hashlib.md5(blobs[name]).hexdigest() == digest, 'Stock MD5 mismatch')
    sq = out/'stock.squashfs'; sq.write_bytes(blobs['recovery-update/rootfs.squashfs'])
    def cat(path): return subprocess.check_output(['unsquashfs', '-cat', str(sq), path])
    raw_demo = cat('release/bin/demo')
    check(sha(raw_demo) == DEMO_SHA, 'Unsupported demo binary')
    # Every allowlisted context must be a window name. The runtime name is the root "name"
    # property of the UI asset, not the asset path, so check the stock rootfs assets directly:
    # a prefix-trimmed typo cannot silently disable a screen this way.
    from ipod import (AUDIT, ARTIST_ALBUMS, ARTIST_PAGE, HOME_PAGE, SETTINGS_ICONS, inc, UI_ASSETS, patch_asset,
                      imagemagick, patch_code, patch_style, patch_word, settings_icon)
    contexts = re.findall(r'"([^"]+)"', (ROOT/'patch/contexts.inc').read_text())
    check(contexts, 'No navigation contexts audited')
    windows = set()
    for line in run('unsquashfs', '-l', sq).splitlines():
        found = re.search(r'/raw/ui/(.+)\.bin$', line)
        if not found: continue
        rel = found.group(1)
        data = cat('release/assets/default/raw/ui/'+rel+'.bin')
        i = data.find(b'name\x00')
        name = data[i+5:data.find(b'\x00', i+5)].decode('utf-8', 'replace') if i >= 0 else ''
        windows.add(name or rel.split('/')[-1])
        # iPod pre-sizes the settings icons, so no UI asset may name one (only native settings code does).
        for icon in SETTINGS_ICONS:
            check(icon.removesuffix('.png').encode() + b'\0' not in data, f'{rel}: names settings icon {icon}')
    check(windows, 'No UI assets in the stock rootfs')
    for name in contexts:
        check(name in windows | PAYLOAD_WINDOWS, f'Context {name} is not a window name in the stock rootfs')
    demo = out/'stock-demo'; demo.write_bytes(raw_demo)
    symbol_table = run('readelf', '-Ws', demo)
    syms = symbols(demo, symbol_table)
    syms.update(PRIVATE_FUNCTIONS)
    header = [f'#define RING_STEP {RING_STEP}']
    for name in GLOBALS:
        check(re.search(rf'\b1\s+OBJECT\s+GLOBAL\s+DEFAULT\s+\d+\s+{name}$',
                        symbol_table, re.M), f'{name}: byte global size mismatch')
    for name, size in CONTEXT_DATA.items():
        check(re.search(rf'\b{size}\s+OBJECT\s+GLOBAL\s+DEFAULT\s+\d+\s+{name}$',
                        symbol_table, re.M), f'{name}: context data size mismatch')
        header.append(f'#define {name} ((const unsigned char *)0x{syms[name]:x}u)')
    # iPod's image hook leaves the settings icons' category colours alone (navigation.c settings_icon).
    names = ''.join(n.removesuffix('.png') + '\\0' for n in SETTINGS_ICONS)
    header.append(f'#define SETTINGS_ICON_NAMES "{names}"')
    # About: the stock firmware's version on its own row, and this build's on the CFW. Version row.
    header += [f'#define STOCK_VERSION "{info[1]}"', f'#define Q2POD_VERSION "{display_version}"']
    (out/'stock.h').write_text('\n'.join(header)+'\n')
    ps = compile_payload(out, ipod)
    payload = (out/'patch.bin').read_bytes()
    check(len(payload) < SCRATCH-BASE, 'Payload overlaps its scratch page')
    check(ps['__scratch_start'] == SCRATCH, 'Scratch state moved')
    check(ps['__scratch_end'] <= SCRATCH + 0x10000, 'Scratch state exceeds its page')
    patched = bytearray(raw_demo)
    def jump(off, name): patched[off:off+8] = struct.pack('<II', 0x08000000 | (ps[name] >> 2), 0)
    for name, (address, replacement) in hooks(ipod).items():
        check(syms[name] == address, f'{name}: callback address mismatch')
        off = fileoff(patched, address)
        prolog = struct.unpack_from('<III', patched, off)
        check(prolog[0] >> 16 == 0x3c1c and prolog[1] >> 16 == 0x279c and
              prolog[2] == 0x0399e021, f'{name}: unexpected PIC prologue')
        low = prolog[1] & 65535
        gp = ((prolog[0] & 65535) << 16) + (low if low < 32768 else low - 65536) + address
        check(gp == GP, f'{name}: unexpected GOT base')
        jump(off, replacement)
    for name, address, replacement, words in [WM_PAINT_LEAF] + [IPOD_LEAF] * ipod:
        off = fileoff(patched, address)
        check(syms[name] == address and struct.unpack_from('<III', patched, off) == words, f'{name}: unexpected code')
        jump(off, replacement)
    if ipod:
        # style_get_color's own bal style_get_gradient, returning to STYLE_COLOR_GRADIENT_RET, stays unmapped.
        ret = inc('STYLE_COLOR_GRADIENT_RET')
        check(struct.unpack_from('<I', patched, fileoff(patched, ret - 8))[0] == 0x04110000 | (IPOD_LEAF[1] - ret + 4) >> 2 & 0xffff,
              'style_get_color: unexpected gradient call')
    from peq import patch_player
    raw_player = cat('usr/bin/hciplayer')
    audio = patch_player(raw_player, out/'peq')
    bluealsa = patch_bluealsa(cat(BLUEALSA))
    (out/'bluealsa').write_bytes(bluealsa)
    (out/'watchdog').write_bytes(patch_watchdog(cat(WATCHDOG)))
    for address, old, new in ARTIST_ALBUMS:
        patch_word(patched, address, old, new, 'artist detail opens on Albums')
    patch_word(patched, *SHUFFLE_CALL, 0x0c000000 | (ps['ringnav_shuffle'] >> 2),
               'shuffle honours Play next')
    patch_word(patched, *DROP_CACHES, 'keep the page cache')
    for address, old in WHEEL_THRESHOLDS:
        patch_word(patched, address, old, old & 0xffff0000 | round((old & 0xffff) * WHEEL_TRAVEL),
                   'wheel travel per tick')
    for address in SORT_TRIMS:
        patch_word(patched, address, 0x04110000 | (syms['toolsTrimLeft'] - address - 4) >> 2 & 0xffff,
                   0x0c000000 | (ps['ringnav_sort_key'] >> 2), 'sort without a leading article')
    # Pin added private entry points as well as every replaced instruction, and the stock bitmap,
    # canvas and slide_menu entries Coverflow's depth renderer calls (docs/internals.md#coverflow-depth).
    for name, original in AUDIT['private_prologues'].items():
        off = fileoff(raw_demo, syms[name])
        check(raw_demo[off:off+12].hex() == original, f'{name}: unexpected stock entry')
    for address, original in AUDIT['event_abi_words'].items():
        off = fileoff(raw_demo, int(address, 16))
        check(raw_demo[off:off+4].hex() == original, f'{address}: unexpected event ABI instruction')
    code_changes = []
    if ipod:
        code_changes = patch_code(patched, ps)
    # Single shared version literal: the updater refuses an equal firmware_v20.info version (update_firmware
    # 0x4f8440, check_otginfo 0x4f8968), so it carries the build tag and stock V1.32 can be flashed back.
    # About's FW. Version row reads it too (0x4bc52c); ringnav_about shows STOCK_VERSION there instead.
    check(patched.count(b'V1.32\0') == 1, 'Version literal is not unique')
    check(len(version) + 1 == len(b'V1.32\0'),
          'VERSION must stay 5 characters; a longer literal shifts every later file offset')
    patched = patched.replace(b'V1.32\0', version.encode()+b'\0')
    patched = patched.replace(*RTC_WRITE)
    check(re.search(r'\.pdr +PROGBITS +0+ +0*%x +0*%x ' % PDR, run('readelf', '-SW', demo)) and
          all(p[1]+p[4] <= PDR[0] for _,p in segments(patched) if p[0] == 1), '.pdr: not the audited section')
    patched[PDR[0]:PDR[0]+PDR[1]] = bytes(PDR[1])
    append_payload(patched, payload, BASE, ps['__scratch_end']-BASE, 'demo')
    (out/'demo').write_bytes(patched)
    # Pseudo-file round trip preserves every original inode's metadata and hardlinks.
    pseudo = out/'root.pseudo'
    run('unsquashfs','-pf',pseudo,sq)
    p = pseudo.read_bytes()
    # mksquashfs takes "/" from the source dir, not the pseudo file; carry stock values over.
    root = re.search(rb'^/ D (\d+) (\d+) (\d+) (\d+)$',p,re.M)
    check(root is not None, 'Missing root pseudo inode')
    t,mode,uid,gid = (x.decode() for x in root.groups())
    rootargs = ['-root-time',t,'-root-mode',mode,'-root-uid',uid,'-root-gid',gid]
    # Paths are passed through a shell by mksquashfs F entries; quote them explicitly.
    def inode(p, path): return re.search(rb'^'+re.escape(path)+rb' R (\d+) (\d+) (\d+) (\d+) .+$',p,re.M)
    def swap_inode(p, path, src):
        line = inode(p, path)
        check(line is not None, f'Missing {path.decode()} pseudo inode')
        return p[:line.start()]+path+b' F '+b' '.join(line.groups())+b' cat '+shlex.quote(str(src)).encode()+p[line.end():]
    p = swap_inode(p, b'release/bin/demo', out/'demo')
    p = swap_inode(p, b'usr/bin/hciplayer', out/'peq/hciplayer')
    p = swap_inode(p, BLUEALSA.encode(), out/'bluealsa')
    p = swap_inode(p, WATCHDOG.encode(), out/'watchdog')
    (out/'S90play').write_bytes(patch_s90play(cat(S90PLAY)))
    p = swap_inode(p, S90PLAY.encode(), out/'S90play')
    logo_data = logo.read_bytes()
    check(jpeg_size(logo_data) == (320, 375), 'Logo must be 320x375 like the stock splash')
    # Package exactly the validated bytes, even if the input is edited during compression.
    logo = out/'logo.jpg'
    logo.write_bytes(logo_data)
    p = swap_inode(p, b'release/assets/default/raw/images/xx/logo.jpg', logo)
    # New inodes, each with its stock image's metadata: the Stock build's Coverflow card icons (menu_music's),
    # the Local Music rows' icons, drawn in assets/icons/ (stock's 52px style) or else a copy of the stock image named
    # (not the copies iPod pre-sizes for Settings), and q2video.
    xx = 'release/assets/default/raw/images/xx/'
    icons = {} if ipod else {n: (n.replace('coverflow', 'music'), (ROOT/'assets/icons'/n).read_bytes()) for n in ICONS}
    drawn = ('podcasts', 'audiobooks', 'books', 'videos')
    icons.update({f'local_{n}.png': (like, (ROOT/'assets/icons'/f'local_{n}.png').read_bytes() if n in drawn
                                     else cat(xx+like)) for n, like in dict(
        shuffle='playset_playmode.png', scrobble='wifiset_wifi.png', podcasts='netservice_dlna.png',
        audiobooks='playset_foldercover.png', photos='playset_covermode.png', books='system_language.png',
        videos='system_display.png').items()})
    added = []
    for path, (like, data) in {**{xx+n: (xx+l, d) for n, (l, d) in icons.items()},
                               HELPER: (HELPER_LIKE, compile_helper(out, cat, 'video', 'q2video', HELPER_LIBS)),
                               BOOT: (HELPER_LIKE, compile_helper(out, cat, 'boot', 'q2boot', HELPER_LIBS[:1]))}.items():
        stock = inode(p, like.encode())
        check(stock is not None, f'Missing stock inode for {path}')
        name = path.rsplit('/', 1)[-1]
        if path not in (HELPER, BOOT): (out/name).write_bytes(data)  # package the hashed bytes, as the logo
        path = path.encode()
        entry = path+b' F '+b' '.join(stock.groups())+b' cat '+shlex.quote(str(out/name)).encode()+b'\n'
        at = p.index(b'# START OF DATA')  # definitions precede the embedded data
        p = p[:at]+entry+p[at:]
        added.append([path, b'R', *stock.groups()])
    removed = []
    for path in STOCK_EQ + (CAROUSEL if ipod else []):
        line = re.search(rb'^'+re.escape(path.encode())+rb' R .+\n', p, re.M)
        check(line is not None, f'Missing stock inode {path}')
        removed.append(line.group().split()[:6])
        p = p[:line.start()]+p[line.end():]
    changed_assets = {}
    assets = ['ui/'+rel for rel in (UI_ASSETS if ipod else [ARTIST_PAGE, HOME_PAGE])] + ['strings/en_US.bin']
    if ipod:
        assets += ['styles/'+rel for rel in AUDIT['styles']]
        # settings icons pre-sized to the rows' SET_ICON, in place, so each keeps its inode metadata
        assets += ['images/'+name for name in SETTINGS_ICONS]
    for rel in assets:
        kind, name = rel.split('/', 1)
        path = 'release/assets/default/raw/' + ('images/xx/'+name if kind == 'images' else rel)
        original = cat(path)
        check(kind != 'strings' or original.count(QUEUE_LABEL[0]) == 1, 'Unexpected queue label')
        data = (original.replace(*QUEUE_LABEL) if kind == 'strings' else
                patch_style(original, AUDIT['styles'][name]) if kind == 'styles' else
                settings_icon(name, original) if kind == 'images' else patch_asset(name, original, ipod))
        target = out/rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        p = swap_inode(p, path.encode(), target)
        changed_assets[path] = dict(original_sha256=sha(original), sha256=sha(data))
    pseudo.write_bytes(p)
    (out/'empty').mkdir()
    newsq = out/'rootfs.squashfs'
    epoch = struct.unpack_from('<I',sq.read_bytes(),8)[0]
    run('mksquashfs',out/'empty',newsq,'-pf',pseudo,'-noappend','-comp','lzo',
        '-b','131072','-Xcompression-level','9','-mkfs-time',epoch,*rootargs,'-processors','1','-no-progress')
    # All inodes, including demo and the logo, keep name/type/mtime/mode/uid/gid (sizes/offsets shift).
    def inodes(image):
        text = subprocess.check_output(['unsquashfs','-pf','-',str(image)]).split(b'\n# START OF DATA')[0]
        return sorted(l.split()[:6] for l in text.splitlines() if l and not l.startswith(b'#'))
    check(inodes(newsq) == sorted([i for i in inodes(sq) if i not in removed]+added),
          'Repacked rootfs metadata differs from stock')
    blobs['recovery-update/rootfs.squashfs'] = newsq.read_bytes()
    # Stock image proves this size fits; do not enlarge beyond its padded size.
    check(len(blobs['recovery-update/rootfs.squashfs']) <= sq.stat().st_size, 'Repacked rootfs exceeds stock size')
    blobs['firmware_v20.info'] = (f'Shanling Q2\n{version}\n'+''.join(
        hashlib.md5(blobs[n]).hexdigest()+'  '+n+'\n' for n in [
            'recovery-update/xImage','recovery-update/rootfs.squashfs'])).encode()
    with tarfile.open(out/'update.tar','w',format=tarfile.GNU_FORMAT) as t:
        for m in meta:
            data = blobs.get(m.name)
            if data is not None: m.size=len(data)
            t.addfile(m,io.BytesIO(data) if data is not None else None)
    manifest = dict(input_zip_sha256=ZIP_SHA, stock_demo_sha256=DEMO_SHA, source_sha256=source,
        demo_sha256=sha(patched), patch_sha256=sha(payload), update_sha256=sha((out/'update.tar').read_bytes()),
        rootfs_sha256=sha(newsq.read_bytes()), kernel_sha256=sha(blobs['recovery-update/xImage']),
        patch_bytes=len(payload), ring_step_pixels=RING_STEP,
        version=version, release_version=VERSION, build_number=build_number, variant=variant, dev=dev, peq=audio, bluealsa_sha256=sha(bluealsa), q2video_sha256=sha((out/'q2video').read_bytes()), q2boot_sha256=sha((out/'q2boot').read_bytes()), compact_code=code_changes, changed_assets=changed_assets, logo_sha256=sha(logo_data),
        patch_symbols={n:hex(v) for n,v in ps.items() if n.startswith('stock_')},
        tools={t:run(t,'--version').splitlines()[0] for t in ['clang','ld.lld','llvm-objcopy']} |
              ({'imagemagick': imagemagick('-version', data=b'').decode().splitlines()[0]} if ipod else {}))
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({k:manifest[k] for k in ['update_sha256','patch_bytes','version']},indent=2))

if __name__ == '__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('zip',type=pathlib.Path)
    ap.add_argument('--out',type=pathlib.Path,default=ROOT/'build')
    ap.add_argument('--logo',type=pathlib.Path,default=ROOT/'assets/boot-logo.jpg',
                    help='320x375 JPEG boot splash (default: assets/boot-logo.jpg)')
    ap.add_argument('--ipod', action='store_true', help='iPod UI: compact local browsing and long Return to Now Playing')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--dev', action='store_true', help='development build: increment the local build number')
    mode.add_argument('--prod', action='store_true', help='production build: increment VERSION’s minor component')
    a=ap.parse_args()
    try:
        # Serialize local numbering and version bumps, including concurrent script invocations.
        if a.dev or a.prod:
            with (ROOT/'.build-number').open('a+') as counter:
                fcntl.flock(counter, fcntl.LOCK_EX)
                # A previous process may have bumped the version while we waited for the lock.
                path = ROOT/'tools/build.py'
                original = path.read_text()
                VERSION = re.search(r"^VERSION = '([0-9]+\.[0-9]+)'$", original, re.M)[1]
                if a.prod:
                    major, minor = map(int, VERSION.split('.'))
                    next_version = f'{major}.{minor + 1}'
                    version_tag(next_version, 'ipod' if a.ipod else 'stock')
                    updated = original.replace(f"VERSION = '{VERSION}'", f"VERSION = '{next_version}'", 1)
                    check(updated != original, 'Could not update VERSION')
                    path.write_text(updated)
                    try:
                        subprocess.run([sys.executable, str(path), str(a.zip),
                                        '--out', str(a.out.resolve()), '--logo', str(a.logo),
                                        *(['--ipod'] if a.ipod else [])], check=True)
                    except BaseException:
                        path.write_text(original)
                        raise
                else:
                    counter.seek(0)
                    previous = counter.read().strip()
                    number = int(previous or '0') + 1
                    version_tag(VERSION, 'ipod' if a.ipod else 'stock', number)
                    # Reserve before building. Failed builds consume a number instead of reusing it.
                    counter.seek(0)
                    counter.truncate()
                    counter.write(f'{number}\n')
                    counter.flush()
                    build(a.zip, a.out.resolve(), a.logo, a.ipod, True, number)
        else:
            build(a.zip, a.out.resolve(), a.logo, a.ipod)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        ap.error(str(exc))
