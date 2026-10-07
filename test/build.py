#!/usr/bin/env python3
"""JPEG checks; optionally pass the stock ZIP to test packaging with a custom logo and quoted paths too."""
import sys; sys.path.insert(0, sys.path[0] + '/../tools')  # tools/ first: test/build.py must import tools/build.py
from build import CAROUSEL, ICONS, QUEUE_LABEL, ROOT, STOCK_EQ, jpeg_size

logo = (ROOT/'assets/boot-logo.jpg').read_bytes()
assert jpeg_size(logo) == (320, 375)
# JPEG permits extra FF fill bytes before a marker.
assert jpeg_size(logo[:2] + b'\xff' + logo[2:]) == (320, 375)
frame = b'\xff\xd8\xff\xc0\x00\x11\x08\x01\x77\x01\x40'
# The stock display_logo reads three bytes per pixel without converting grayscale/CMYK.
# It also requires the documented 8-bit baseline format.
def sof(marker=0xc0, precision=8, components=3):
    return (b'\xff\xd8\xff' + bytes([marker]) + (8+3*components).to_bytes(2,'big') +
            bytes([precision]) + b'\x01\x77\x01\x40' + bytes([components]) +
            b''.join(bytes([i+1,0x11,0]) for i in range(components)))
assert jpeg_size(sof()) == (320,375)
for data in (b'', b'not a JPEG', frame, frame + bytes(8),
             b'\xff\xd8\xff', b'\xff\xd8\xff\xe0\x00',
             b'\xff\xd8\xff\xe0\x00\x01', b'\xff\xd8\xff\xd9' + logo[2:],
             sof(components=1), sof(components=4), sof(precision=12),
             sof(marker=0xc2), sof(marker=0xc3)):
    try:
        jpeg_size(data)
    except ValueError:
        continue
    raise AssertionError(f'Accepted malformed JPEG header: {data!r}')
print('JPEG header regression checks passed.')

def boot_check():
    """S90play's dual boot (build.py BOOT_HOOK) under the host sh, with stand-in programs."""
    import subprocess, tempfile, pathlib
    from build import BOOT_HOOK
    with tempfile.TemporaryDirectory(prefix='q2-boot-') as tmp:
        r = pathlib.Path(tmp)
        script = BOOT_HOOK[1].decode().replace(' &\n', '\n')
        for old in ('/mnt/', '/tmp/mmc_add', '/usr/bin/q2boot', '/release/bin/demo'):
            script = script.replace(old, f'{r}{old}')
        script = script.replace('usleep 200000', ':')
        for d in ('mnt/data', 'mnt/mmc/.rockbox', 'tmp', 'usr/bin', 'release/bin'): (r/d).mkdir(parents=True)
        def exe(path, body): (r/path).write_text('#!/bin/sh\n' + body + '\n'); (r/path).chmod(0o755)
        exe('release/bin/demo', f'echo demo >> {r}/ran')
        rb, target = r/'mnt/mmc/.rockbox/rockbox', r/'mnt/data/boot-target'
        def boot(held, card, code=81):
            exe('usr/bin/q2boot', 'exit ' + ('0' if held else '1'))
            if card: exe('mnt/mmc/.rockbox/rockbox', f'pwd >> {r}/ran; exit {code}')
            else: rb.unlink(missing_ok=True)
            (r/'ran').write_text('')
            subprocess.run(['sh', '-c', script], check=True)
            return (r/'ran').read_text().split(), target.exists() and target.read_text().strip()
        rockbox = [str(rb.parent), 'demo']
        assert boot(False, False) == (['demo'], False)          # no Rockbox on the card: Q2 Pod
        assert boot(False, True, 1) == (rockbox, False)         # Rockbox by default; a crash starts Q2 Pod
        assert (r/'mnt/mmc/.rockbox/rockbox.log').read_text() == 'exit 1\n'
        assert boot(False, True) == (rockbox, False)            # Boot stock OS (0x51): Q2 Pod starts, nothing saved
        assert boot(True, True, 1) == (['demo'], False)         # Play/Pause held: Q2 Pod, this session
        assert boot(False, True, 1) == (rockbox, False)         # the next power-on is Rockbox again
        target.write_text('stock\n')                            # a stale V8.4 choice is ignored
        assert boot(False, True, 1) == (rockbox, 'stock')
        assert boot(True, True, 1) == (['demo'], 'stock')
    print('Dual boot: Rockbox by default, one-session Q2 Pod, stale choice ignored and card fallback passed.')
boot_check()

def validate_assets(directory):
    import functools, json, re, struct, subprocess
    from build import sha, run, fileoff, symbols, BLUEALSA, AAC_44K1, IPOD_HOOKS, IPOD_LEAF, WM_PAINT_LEAF, HELPER, HELPER_LIKE, BOOT, BOOT_HOOK, S90PLAY, RTC_WRITE, WATCHDOG, WATCHDOG_SLEEP, DROP_CACHES, WHEEL_THRESHOLDS, SYSTEM_SETTINGS_TABLE, PDR
    from ipod import (AUDIT, BOTTOM, CHEVRON_W, CONFIRM, VOLUME, QUICK_SETTINGS, QS_TOP, QS_LABEL_GAP, QS_LABEL_H,
                      QS_LABEL_W, QS_ROW_GAP, QS_PITCH, QS_BAR, QS_TOUCH, QS_EDGE, QS_SUN, HOME_LABEL_END, HOME_LIST_W, HOME_TEXT_X, HOME_TOP, PITCH, ARTIST_PAGE, HOME_PAGE, HOME_ROW, HOME_ROWS, NAVBAR_ONLY, PLAYING_PAGE, SET_ROW, SET_ROWS, SET_TOP, UI_ASSETS,
                      NP_BAR, NP_TOP, STATUS_BAR, STATUS_HIDDEN, STATUS_LEFT, STATUS_MARGIN, STATUS_RIGHT, CLOCK_MIN, corner_inset, corner_x,
                      SET_ICON, SET_STOCK_ICON, SETTINGS_ICONS, decode, imagemagick, inc, png_header, settings_icon, walk,
                      patch_asset, patch_code, patch_style, style_props, SLIDE)
    manifest = json.loads((directory/'manifest.json').read_text())
    ipod = manifest['variant'] == 'ipod'
    stock = (directory/'stock-demo').read_bytes()
    demo = (directory/'demo').read_bytes()
    # All original executable bytes outside the reviewed hooks, version and compact sites
    # must remain stock; the payload and ELF mapping are independently hashed by the runner.
    if not ipod:
        for group in [*AUDIT['immediates'], AUDIT['row_layout_calls'],
                      {'sites': [('0x522410', '0x0320f809'), ('0x523de0', '0x0320f809')]}]:
            for address, _ in group['sites']:
                off = fileoff(stock, int(address, 16))
                assert demo[off:off+4] == stock[off:off+4]
    assert manifest['version'].encode()+b'\0' in demo
    # .pdr (build.py PDR), past every LOAD segment, ships zeroed.
    assert demo[PDR[0]:PDR[0]+PDR[1]] == bytes(PDR[1]) and any(stock[PDR[0]:PDR[0]+PDR[1]])
    # The RTC is written in UTC: the one hwclock -w literal gains -u in its own padding.
    assert demo.index(RTC_WRITE[1]) == stock.index(RTC_WRITE[0])
    # The slide hint names a stock animator and gives it a duration (a missing one means stock's 500 ms).
    assert re.fullmatch(r'htranslate\(duration=\d+\)', SLIDE) and b'\0htranslate\0' in stock
    # iPod alone jumps from these entry points to its payload (build.py pins the leaf's words);
    # Stock keeps all of them stock.
    for address, name in [*IPOD_HOOKS.values(), IPOD_LEAF[1:3]]:
        off = fileoff(stock, address)
        want = stock[off:off+8]
        if ipod: want = (0x08000000 | symbols(directory/'patch.elf')[name] >> 2).to_bytes(4, 'little') + bytes(4)
        assert demo[off:off+8] == want
    # Both jump from window_manager_paint (Videos).
    off = fileoff(stock, WM_PAINT_LEAF[1])
    assert demo[off:off+8] == (0x08000000 | symbols(directory/'patch.elf')[WM_PAINT_LEAF[2]] >> 2).to_bytes(4, 'little') + bytes(4)
    changed = manifest['changed_assets']
    xx = 'release/assets/default/raw/images/xx/'
    assert set(changed) == {'release/assets/default/raw/ui/'+p for p in (UI_ASSETS if ipod else [ARTIST_PAGE, HOME_PAGE])} | {
        'release/assets/default/raw/styles/'+p for p in (AUDIT['styles'] if ipod else [])} | {
        xx+n for n in (SETTINGS_ICONS if ipod else [])} | {'release/assets/default/raw/strings/en_US.bin'}
    def read(image, rel):
        return subprocess.check_output(['unsquashfs', '-cat', str(directory/image), rel])
    # Now Playing's queue reads "Queue" (QUEUE_LABEL); nothing else in the string table moves.
    strings = 'release/assets/default/raw/strings/en_US.bin'
    old, new = read('stock.squashfs', strings), read('rootfs.squashfs', strings)
    assert new == old.replace(*QUEUE_LABEL) and new != old and len(new) == len(old)
    # iPod settings icons: the audited 52px artwork, packaged as SET_ICON RGBA with the same transparency
    # and, on a plain background, the same average colour; Stock keeps them stock.
    def mean(png, bg):
        cmd = ['png:-', '-background', bg, '-flatten', '-format', '%[fx:mean.r],%[fx:mean.g],%[fx:mean.b]', 'info:']
        return [float(v) for v in imagemagick(*cmd, data=png).split(b',')]
    for name, digest in SETTINGS_ICONS.items():
        old, new = read('stock.squashfs', xx+name), read('rootfs.squashfs', xx+name)
        assert sha(old) == digest and png_header(old) == (SET_STOCK_ICON, SET_STOCK_ICON, 8, 6), name
        if not ipod:
            assert new == old, name
            continue
        assert changed[xx+name] == dict(original_sha256=digest, sha256=sha(new)) and new == settings_icon(name, old), name
        assert png_header(new) == (SET_ICON, SET_ICON, 8, 6), name
        try: settings_icon(name, old[:-1] + b'x')
        except ValueError: pass
        else: raise AssertionError(f'{name}: accepted a changed icon')
        for bg in ('#000000', '#6e6e6e'):  # the list, and the Graphite selection bar
            assert max(abs(a - b) for a, b in zip(mean(old, bg), mean(new, bg))) < 0.02, (name, bg)
    # Videos' player: an ELF with the stock binaries' ABI flags (nan2008, o32, mips32r2).
    helper = read('rootfs.squashfs', HELPER)
    assert sha(helper) == manifest['q2video_sha256'] and helper[:4] == b'\x7fELF'
    assert helper[36:40] == read('stock.squashfs', HELPER_LIKE)[36:40]
    # Rockbox's dual boot: q2boot likewise, and S90play differs from stock only in starting demo.
    boot = read('rootfs.squashfs', BOOT)
    assert sha(boot) == manifest['q2boot_sha256'] and boot[:4] == b'\x7fELF' and boot[36:40] == helper[36:40]
    assert read('rootfs.squashfs', S90PLAY) == read('stock.squashfs', S90PLAY).replace(*BOOT_HOOK)
    # bluealsa differs from stock only in the AAC 44.1 kHz bit.
    old, new = read('stock.squashfs', BLUEALSA), read('rootfs.squashfs', BLUEALSA)
    assert len(new) == len(old) and [i for i in range(len(old)) if old[i] != new[i]] == [AAC_44K1]
    assert new[AAC_44K1] == 0 and manifest['bluealsa_sha256'] == sha(new)
    # The crash watchdog only sleeps longer; check_mem_thd's drop_caches write is branched over.
    old, new = read('stock.squashfs', WATCHDOG), read('rootfs.squashfs', WATCHDOG)
    assert new == old.replace(*WATCHDOG_SLEEP) and new != old
    off = fileoff(stock, DROP_CACHES[0])
    assert struct.unpack_from('<I', stock, off)[0] == DROP_CACHES[1] and struct.unpack_from('<I', demo, off)[0] == DROP_CACHES[2]
    off = fileoff(stock, SYSTEM_SETTINGS_TABLE)
    assert struct.unpack_from('<12I', stock, off) == tuple(range(12))
    assert struct.unpack_from('<12I', demo, off) == (1, 3, 4, 8, 7, 2, 6, 5, 0, 9, 11, 10)
    # The knob takes 1.2 times stock's travel per tick: 24 for a gesture's first, 12 after.
    for (address, old), travel in zip(WHEEL_THRESHOLDS, (24, 12, 24)):
        off = fileoff(stock, address)
        assert struct.unpack_from('<I', stock, off)[0] == old and struct.unpack_from('<I', demo, off)[0] == old & 0xffff0000 | travel
    # hciplayer's code (.text) differs from stock only in the VBR scan branch, now a nop, and the 48 kHz EQ gates.
    from peq import VBR_SCAN, EQ_RATE_GATES
    old, new = read('stock.squashfs', 'usr/bin/hciplayer'), read('rootfs.squashfs', 'usr/bin/hciplayer')
    off = fileoff(old, VBR_SCAN)
    gates = [fileoff(old, a) for a, _, _ in EQ_RATE_GATES]
    differ = [i for i in range(0x3e40, 0x485250) if old[i] != new[i]]
    assert set(differ) <= {*range(off, off+4), *(i for g in gates for i in range(g, g+4))} and not any(new[off:off+4])
    assert [struct.unpack_from('<I', new, fileoff(old, a))[0] for a, _, _ in EQ_RATE_GATES] == [p for _, _, p in EQ_RATE_GATES]
    # Include every excluded UI screen and saved-preference defaults in byte parity checks.
    paths = [l.removeprefix('squashfs-root/') for l in run('unsquashfs', '-l', directory/'stock.squashfs').splitlines()
             if ('/raw/ui/' in l or '/raw/styles/' in l) and l.endswith('.bin') or l.endswith('/config.ini')]
    names = set(run('unsquashfs', '-l', directory/'rootfs.squashfs').splitlines())
    assert not {'squashfs-root/'+rel for rel in STOCK_EQ} & names, 'Stock EQ assets remain'
    # iPod drops the carousel images and adds no Coverflow icons; Stock keeps both.
    icons = {'squashfs-root/release/assets/default/raw/images/xx/'+n for n in ICONS}
    carousel = {'squashfs-root/'+rel for rel in CAROUSEL}
    assert (carousel & names == (set() if ipod else carousel)) and (icons & names == (set() if ipod else icons))
    # Colours the firmware will paint (inline style props, else the theme entry for the widget's style):
    # no screen may gain a light background or border, or dark text, that stock did not already paint.
    def palette(path):
        return {(w, s, p): v.hex() for w, s, state, p, _, v in style_props(read(path, 'release/assets/default/raw/styles/default.bin'))
                if state == 'normal' and p in ('bg_color', 'border_color', 'text_color')}
    themes = palette('stock.squashfs'), palette('rootfs.squashfs')
    def painted(root, theme):
        out = set()
        for kind, _, props, _ in walk(root):
            style = props.get('style', 'default')
            for p in ('bg_color', 'border_color', 'text_color'):
                v = props.get('style:normal:'+p, '').lstrip('#').lower() or theme.get((kind, style, p))
                if v: out.add((kind, props.get('name', ''), p, v if len(v) == 8 else v+'ff'))
        return out
    def luma(v): return sum(k*int(v[i:i+2], 16) for k, i in ((0.2126, 0), (0.7152, 2), (0.0722, 4))) / 255
    def jarring(c):
        kind, _, p, v = c
        if int(v[6:], 16) <= 0x40: return False
        return luma(v) < 0.25 if p == 'text_color' else luma(v) > 0.35 and kind != 'image'
    # iPod: text and icons must clear the glass's rounded corners (ipod.CORNER_R); backgrounds,
    # bars and full-bleed art may reach into them. Content is a label's font-high band, an image drawn centred at its
    # size, a slider's bar, else the widget; the window clips it, and row layouts place their children.
    # List rows scroll, so only fixed widgets are checked.
    fonts = {(w, s): int.from_bytes(v, 'little') for w, s, state, p, _, v in style_props(
        read('rootfs.squashfs', 'release/assets/default/raw/styles/default.bin')) if state == 'normal' and p == 'font_size'}
    @functools.cache
    def image_size(name):
        try: return struct.unpack('>II', read('rootfs.squashfs', f'release/assets/default/raw/images/xx/{name}.png')[16:24])
        except subprocess.CalledProcessError: return None
    def content(kind, props, x, y, w, h):
        if kind in ('label', 'hscroll_label'):
            size = int(props.get('style:normal:font_size') or fonts.get((kind, props.get('style', 'default')), 18))
            return x, y if props.get('style:normal:text_align_v') == 'top' else y + (h - size) // 2, w, size
        if kind == 'slider':
            bar = int(props.get('bar_size', h))
            return x, y + (h - bar) // 2, w, bar
        if kind == 'progress_bar': return x, y, w, h
        name = props.get('image') or props.get('style:normal:bg_image')
        if kind not in ('image', 'gif', 'image_animation') or not name: return None
        draw = props.get('draw_type') if 'image' in props else props.get('style:normal:bg_image_draw_type', 'center')
        if draw == 'fill': return None  # full-bleed art, like a background: the glass rounds its corners
        size = image_size(name) if draw == 'center' else None
        return (x + (w - size[0]) // 2, y + (h - size[1]) // 2, *size) if size else (x, y, w, h)
    def corners(short, n, x0, y0, window, slot=None):
        kind, (x, y, w, h), props, children = n
        if props.get('visible') == 'false' or kind in ('list_item', 'table_row'): return  # hidden, or rows that scroll
        x, y, w, h = slot or (x, y, w, h)
        if at := re.match(r'default\(x=(-?\d+),y=(-?\d+),', props.get('self_layout', '')):
            x, y = int(at[1]), int(at[2])
        x, y = x0 + x, y0 + y
        if box := content(kind, props, x, y, w, h):
            bx, by, bw, bh = box
            top, bottom = max(by, window[0]), min(by + bh, window[1])
            inset = max(corner_inset(top), corner_inset(bottom))
            assert bx + bw <= 0 or bx >= 375 or top >= bottom or inset <= bx and min(bx + bw, 375) <= 375 - inset, (
                short, props.get('name'), box, inset)
        slots, row = {}, re.fullmatch(r'default\(r=1,c=0,(a=right,)?xm=(\d+),s=(\d+)\)', props.get('children_layout', ''))
        if row:  # AWTK's row layout, every child shown
            gap, cx = int(row[3]), int(row[2])
            if row[1]: cx = w - cx - sum(c[1][2] for c in children) - gap * (len(children) - 1)
            for c in children:
                slots[id(c)] = (cx, 0, c[1][2], h)
                cx += c[1][2] + gap
        for c in children: corners(short, c, x, y, window, slots.get(id(c)))
    for rel in paths:
        if rel in STOCK_EQ: continue
        original, new = read('stock.squashfs', rel), read('rootfs.squashfs', rel)
        if '/raw/ui/' in rel and new[:4] == bytes.fromhex('12122211'):
            now = painted(decode(new), themes[1])
            bad = [c for c in now - painted(decode(original), themes[0]) if jarring(c)
                   and (c[2] != 'text_color' or c[0] in ('label', 'hscroll_label', 'button', 'edit', 'tab_button'))]
            # iPod rows are transparent, so a light list container stock hid behind them would show.
            bad += [c for c in now if ipod and c[2] == 'bg_color' and jarring(c)
                    and c[0] in ('list_view', 'list_item', 'scroll_view', 'table_view', 'table_client', 'view')]
            assert not bad, (rel, bad)
        if rel not in changed:
            assert new == original, rel
            continue
        assert changed[rel] == dict(original_sha256=sha(original), sha256=sha(new))
        if '/raw/styles/' in rel:
            audit = AUDIT['styles'][rel.split('/raw/styles/')[1]]
            assert new == patch_style(original, audit) and len(new) == len(original)
            wrong = [*audit['edits'][0][:3], 'ffffffff', *audit['edits'][0][4:]]
            try:
                patch_style(original, dict(audit, edits=[wrong]))
                raise AssertionError('Accepted a wrong old style value')
            except ValueError:
                pass
            continue
        short = rel.split('/raw/ui/')[1]
        assert new == patch_asset(short, original, ipod), short
        root = decode(new)
        if short == VOLUME:  # iPod only: no highlight, so nothing under it dims; otherwise stock
            plain = decode(original); del plain[2]['highlight']
            assert root == plain
            continue
        # iPod: browsing, settings and the PEQ editor slide; Home, Now Playing, the bar and dialogs don't.
        slides = ipod and short not in (HOME_PAGE, STATUS_BAR, PLAYING_PAGE, QUICK_SETTINGS, CONFIRM)
        expected_hint = SLIDE if slides else None
        if short in AUDIT['selection_assets'] and root[0]=='dialog':
            expected_hint = decode(original)[2].get('anim_hint')
        assert root[2].get('anim_hint') == expected_hint, short
        if ipod:  # screen coordinates: the bar, full-screen dialogs (quick settings slides down from -320), windows
            origin, window = {STATUS_BAR: (0, (0, 30)), QUICK_SETTINGS: (320, (0, 320)), CONFIRM: (0, (0, 320))}.get(short, (30, (30, 320)))
            if root[0]=='dialog' and short in AUDIT['selection_assets']: origin, window = 0, (0, 320)
            corners(short, root, 0, origin, window)
        if short == QUICK_SETTINGS:  # iPod only: the stock 4x2 grid, even label areas, a slim brightness track
            menu, light = root[3]
            icons = [n for n in menu[3] if n[0] == 'image']; labels = [n for n in menu[3] if n[0] == 'label']
            assert len(icons) == len(labels) == 8 and menu[1][1] >= QS_TOP
            assert sorted({n[1][0] for n in icons}) == sorted({n[1][0] for n in decode(original)[3][0][3] if n[0] == 'image'})
            for icon, label in zip(icons, labels):
                x, y, w, h = icon[1]
                # The same 16px two-line area, top-aligned, centred under its icon and clear of the next row.
                assert label[1] == [x + (w - QS_LABEL_W) // 2, y + h + QS_LABEL_GAP, QS_LABEL_W, QS_LABEL_H]
                assert label[2]['style'] == 's_label_white16c' and label[2]['style:normal:text_align_v'] == 'top'
                assert 2 * (16 + 2) <= QS_LABEL_H and label[2]['line_wrap'] == label[2]['word_wrap'] == 'true'
            assert {n[1][1] for n in icons} == {0, QS_PITCH}
            slider, dim, bright = light[3]
            assert light[1][1] == menu[1][1] + menu[1][3] + QS_ROW_GAP and light[1][1] + light[1][3] <= 320 - 16
            assert slider[2]['bar_size'] == str(QS_BAR) and slider[1][3] == QS_TOUCH >= 44 and slider[2]['slide_with_bar'] == 'true'
            assert not [k for k in slider[2] if k.endswith((':bg_image', ':fg_image', ':icon'))]
            assert slider[2]['style'].encode() not in read('rootfs.squashfs', 'release/assets/default/raw/styles/default.bin')
            assert dim[1][0] == QS_EDGE and bright[1][0] + bright[1][2] == 375 - QS_EDGE and dim[2]['image'] == 'drop_lighleft'
            assert dim[1][0] + QS_SUN < slider[1][0] and slider[1][0] + slider[1][2] < bright[1][0]
            continue
        if short == CONFIRM:  # labeled full-width actions, safely defaulting to Cancel
            cancel, enter = root[3]
            assert cancel[0] == enter[0] == 'button'
            assert cancel[1] == [0,208,375,48] and enter[1] == [0,256,375,48]
            assert [n[2]['text'] for n in root[3]] == ['Cancel','Continue']
            assert [n[2]['name'] for n in root[3]] == ['img_cancel','img_enter']
            assert all(not any(k.endswith(':bg_image') for k in n[2]) for n in root[3])
            continue
        from ipod import SLIDER_EDITORS
        if short in SLIDER_EDITORS:
            title,value,slider=SLIDER_EDITORS[short]
            named={n[2].get('name'):n for n in walk(root)}
            assert named['label_editor_title'][2]['text']==title
            assert named[value][1]==[40,64,295,56] and named[value][2]['style:normal:font_size']=='40'
            bar=named[slider]
            assert bar[1]==[40,146,295,48] and bar[2]['bar_size']=='8'
            assert bar[2]['slide_with_bar']=='true'
            assert not any(k.endswith((':bg_image',':fg_image',':icon')) for k in bar[2])
            assert all(named[n][2]['visible']=='false' for n in ('img_dec','img_add'))
            assert named['label_editor_done'][2]['text']=='Centre to finish'
            continue
        if short == HOME_PAGE and not ipod:  # only the Coverflow card is added
            cards = [n[2]['name'] for n in root[3][0][3]]
            assert cards[:3] == ['btn_playing', 'btn_localmusic', 'btn_coverflow'] and len(cards) == 7, cards
            assert root[3][0][3][1][3][1][2]['text'] == 'Library'  # literal: stock skips label_library
            continue
        if short == HOME_PAGE:  # seven rows with the stock names, beside the art; bytes equal patch_asset above
            (lv, lg, _, [sv]), art = root[3]
            assert lv == 'list_view' and sv[0] == 'scroll_view' and art[2]['name'] == 'img_homeart'
            # The art fills the right panel below the status bar; the payload fits and crops it.
            assert art[1] == [HOME_LIST_W, 0, 375 - HOME_LIST_W, 290] and art[2]['draw_type'] == 'fill'
            assert [r[2]['name'] for r in sv[3]] == ['btn_'+n for n in HOME_ROWS] and HOME_ROWS[2] == 'coverflow'
            for name, (_, _, _, (label, image)) in zip(HOME_ROWS, sv[3]):
                assert label[2]['name'] == ('label_library' if name == 'localmusic' else 'label_'+name)
                assert image[2] == {'name': 'img_'+name, 'clickable': 'true'}
                # Whole English labels ("Playback Setting", 149px), ending before the chevron's glyph.
                assert label[1][0] + label[1][2] == image[1][2] - HOME_LABEL_END and label[1][2] >= 149
            assert [r[3][0][2].get('text') for r in sv[3]][1:3] == ['Library', 'Coverflow']
            assert lg == [0, HOME_TOP, HOME_LIST_W, 7*HOME_ROW] and HOME_TOP + 7*HOME_ROW <= BOTTOM - HOME_TOP
            assert b'menu_' not in new and b'slide_menu' not in new
            # Full (coverflow_home_layout): rows end at HOME_FULL_ROW, so the chevron's glyph (x 20 to 31,
            # y 16 to 34 of list_into, centred on the row) mirrors the labels' margin and, on the last
            # row, clears the bottom-right corner as the label clears the bottom-left.
            glyph_end = inc('HOME_FULL_ROW') - CHEVRON_W + 31
            glyph_y = 30 + HOME_TOP + 6*HOME_ROW + (HOME_ROW - 50) // 2 + 16
            assert 375 - glyph_end == HOME_TEXT_X >= corner_x(glyph_y, 34 - 16)
            continue
        if short == STATUS_BAR:  # iPod only: play state left, title between, four icons right
            left, right, *rest = root[3]
            assert [n[2]['name'] for n in left[3]] == STATUS_LEFT and [n[2]['name'] for n in right[3]] == STATUS_RIGHT
            title = rest.pop()
            assert title[0] == 'hscroll_label' and title[2]['name'] == 'label_clock'
            x, _, w, _ = title[1]
            assert x + w/2 == 375/2 and w >= CLOCK_MIN  # centred on the screen, clear of both groups (corners below)
            assert inc('CLOCK_EDGE') >= corner_x((30 - 20) // 2, 20) and x >= inc('CLOCK_EDGE')
            assert fonts[('hscroll_label', title[2]['style'])] == 20
            assert all(v[2]['children_layout'].endswith(f'xm={STATUS_MARGIN},s=5)') for v in (left, right))
            assert [n[2]['name'] for n in rest] == STATUS_HIDDEN and all(n[1][0] + n[1][2] < 0 for n in rest)
            # The Battery setting's percentage and payload battery start hidden (navigation.c bar_sync).
            pct, slot = right[3][2:4]
            assert pct[1][2] == inc('BATT_PCT_W') and pct[2]['visible'] == 'false' and pct[2]['style:normal:text_align_h'] == 'right' and pct[2]['style:normal:font_size'] == str(inc('BATT_PCT_PX'))
            assert slot == ['view', [0, 0, inc('BATT_BODY_W') + inc('BATT_NUB_W'), 0], {'name': 'view_battery', 'visible': 'false'}, []]
            continue
        if short == PLAYING_PAGE:  # iPod only: see the sketch in docs/ipod.md
            named = {n[2].get('name'): n for n in walk(root)}
            assert {n[2].get('name') for n in walk(decode(original))} < set(named)  # stock names kept
            assert [n[2].get('name') for n in root[3]] == ['view_buttons', 'label_playtime', 'label_playlen', 'label_ipod_remain',
                                                           'slide_view_view', 'slider_play', 'img_repeata', 'img_repeatb', 'image_wait', 'label_ipod_control']
            control = named['label_ipod_control']
            assert control[1] == [NP_BAR[0],240,NP_BAR[2],30] and control[2]['visible'] == 'false'
            assert control[2]['style:normal:text_color'] == '#ffffff'
            assert control[2]['style:normal:text_align_h'] == 'center' and control[2]['style:normal:text_align_v'] == 'middle'
            pos = named['label_ipod_pos'][1]
            assert pos[0] + pos[2] == named['img_fav'][1][0] and named['img_return'][1][0] < 0
            icons = [named[n][1] for n in ('img_fav', 'img_more')]
            assert [g[1:] for g in icons] == [[0, 50, 40]]*2 and icons[1][0] - icons[0][0] == 50
            assert named['img_playmode'][1][0] < 0 and named['img_playmode'][2]['visible'] == 'false'
            assert named['img_playmode'][2]['enable'] == 'false'
            album = named['view_album'][3]
            assert [n[2]['name'] for n in album] == ['img_cover', 'img_playstate', 'scrlabel_title', 'scrlabel_artist', 'label_ipod_album']
            # 16px outer margins, 12px from the art to the text, the text column 165px wide; the title larger.
            assert [n[1] for n in album] == [[16, 10, 166, 166], [39, 33, 120, 120], [194, 55, 165, 28], [194, 87, 165, 20], [194, 111, 165, 20]]
            assert album[2][2]['style:normal:font_size'] == '22' and album[3][2]['style:normal:font_size'] == '16'
            assert pos[0] == album[0][1][0] == 16 and 375 - 16 == album[2][1][0] + album[2][1][2]
            # Distinct bands: top row, art, page dots, bar (A-B markers y 250 to 260), then the times.
            dots = named['slide_indicator1'][1]
            assert NP_TOP + 10 + 166 < NP_TOP + dots[1] and NP_TOP + dots[1] + 10 < NP_BAR[1] - 1
            assert named['slide_view'][1] == [0, 0, 375, 186] and named['view_lrc'][2]['self_layout'].startswith('default(x=75,')
            slider = named['slider_play']
            assert slider[1] == [NP_BAR[0], 240, NP_BAR[2], 30] and slider[2]['bar_size'] == '8' and slider[2]['slide_with_bar'] == 'true'
            assert not [k for k in slider[2] if k.endswith((':bg_image', ':fg_image', ':icon'))]
            assert {v for k, v in slider[2].items() if k.endswith('_color')} == {'#1c1c1c', '#6e6e6e'}
            assert {v for k, v in slider[2].items() if k.endswith(':round_radius')} == {'4'}  # a capsule
            # No theme style of that name, so no thumb icon: stock fills exactly to the value.
            assert slider[2]['style'].encode() not in read('rootfs.squashfs', 'release/assets/default/raw/styles/default.bin')
            played, remain = (named[n][1] for n in ('label_playtime', 'label_ipod_remain'))
            assert played[1:] == remain[1:] == [265, 80, 16] and played[0] == 375 - remain[0] - 80 and 250 + 10 < 265
            assert named['label_playlen'][2]['visible'] == 'false'
            assert {v for n in ('label_playtime', 'label_ipod_remain') for k, v in named[n][2].items() if k.endswith(':text_color')} == {'#AAAAAA'}
            # Stock places the A-B markers at y 250 and x = 50 + t * 290 / length; iPod's follow its bar.
            assert [int.from_bytes(demo[fileoff(demo, a):fileoff(demo, a)+4], 'little') for a in (0x52a318, 0x52a330)] == [
                0x24020000 | NP_BAR[2], 0x24420000 | NP_BAR[0]]
            continue
        if short in AUDIT['selection_assets']:
            old_nodes, new_nodes = list(walk(decode(original))), list(walk(root))
            assert len(old_nodes) == len(new_nodes)
            for old, node in zip(old_nodes, new_nodes):
                assert old[0] == node[0] and old[2].get('name') == node[2].get('name')
                if node[0] in ('button', 'list_item'):
                    assert {v for k, v in node[2].items() if k.endswith(':bg_color')} == {'#00000000'}
                    assert {v for k, v in node[2].items() if k.endswith(':border_width')} == {'0'}
            if root[0] == 'window':
                nav = next(n for n in root[3] if n[2].get('name') == 'view_navbar')
                assert nav[2]['visible'] == nav[2]['enable'] == 'false'
            continue
        if short == ARTIST_PAGE:
            assert [n[2]['value'] for n in walk(root) if n[0] == 'pages'] == ['1'], 'Artist page must show Albums'
        if short in AUDIT['slide_only']:  # nothing but the hint changes
            root[2].pop('anim_hint')
            assert root == decode(original), short
            continue
        nav = next(n for n in root[3] if n[2].get('name') == 'view_navbar')
        if short in NAVBAR_ONLY:  # content moves up 50; lists hold four whole SET_ROW rows from SET_TOP
            for old, node in zip(decode(original)[3], root[3]):
                if node is nav: continue
                if node[0] == 'list_view':
                    assert node[1] == [0, SET_TOP, 375, SET_ROWS * SET_ROW] and SET_TOP + SET_ROWS * SET_ROW <= BOTTOM, short
                    assert node[2] == {**old[2], 'default_item_height': str(SET_ROW)} and node[3] == old[3], short
                    continue
                assert node[1][1] == old[1][1] - 50 and node[1][3] == old[1][3] and node[2] == old[2], short
        assert not ipod or nav[2]['visible'] == 'false' and nav[2]['enable'] == 'false'
        assert not ipod or not [v for n in walk(root) if n[0] in ('button', 'list_item', 'table_row')
                                for k, v in n[2].items() if k.endswith(':bg_color') and v == '#000000'], 'Opaque inline row background'
        old_nodes, new_nodes = list(walk(decode(original))), list(walk(root))
        assert len(old_nodes) == len(new_nodes)
        for old, node in zip(old_nodes, new_nodes):
            for key, value in old[2].items():
                if 'font' in key or key == 'style': assert node[2][key] == value
        if short in ('folder_page.bin', 'localmusic_page.bin', 'localmusic/localclass_page.bin', 'localmusic/playlist_page.bin'):
            surface = next(n for n in root[3] if n[0] in ('list_view', 'table_view'))
            assert surface[1][1:] == [0, 375, BOTTOM], 'Lists must fill the client area'
            assert 4*PITCH <= BOTTOM, 'Four complete rows must fit'
        # Corrupt inputs must be rejected, never silently patched.
        try: patch_asset(short, original[:-1]+b'x', ipod)
        except ValueError: pass
        else: raise AssertionError('Accepted a changed asset')
    if ipod:
        payload_symbols = symbols(directory/'patch.elf')
        for address in [AUDIT['immediates'][0]['sites'][0][0], '0x522410', '0x523de0', AUDIT['row_layout_calls']['sites'][0][0],
                        hex(inc('LIST_VIEW_LAYOUT_SLOT'))]:
            damaged = bytearray(stock)
            damaged[fileoff(stock, int(address, 16))] ^= 1
            try: patch_code(damaged, payload_symbols)
            except ValueError: pass
            else: raise AssertionError(f'Accepted a changed instruction at {address}')
    print(f'{manifest["variant"]}: asset geometry, exclusion parity and mismatch rejection passed.')

if __name__ == '__main__':
    import argparse, json, pathlib, subprocess, tempfile
    from unittest.mock import patch
    from build import build, run, sha
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('zip',type=pathlib.Path,nargs='?')
    ap.add_argument('--build', type=pathlib.Path)
    args=ap.parse_args()
    if args.build: validate_assets(args.build)
    if args.zip:
        # Reproducibility and update.tar's MD5s are release.py package's; this covers the logo and quoting.
        for ipod in (False, True):
          with tempfile.TemporaryDirectory(prefix='q2-package-') as tmp:
              root=pathlib.Path(tmp)
              custom=root/"custom ' logo.jpg"
              custom.write_bytes(logo)
              def change_source(*command):
                  if command[0]=='mksquashfs': custom.write_bytes(b'edited during compression')
                  return run(*command)
              a=root/"build ' a"
              with patch('build.run',side_effect=change_source):
                  build(args.zip,a,custom,ipod)
              validate_assets(a)
              manifest=json.loads((a/'manifest.json').read_text())
              assert manifest['logo_sha256']==sha(logo)
              for rel,expected in [('release/assets/default/raw/images/xx/logo.jpg',logo),
                                   ('release/bin/demo',(a/'demo').read_bytes())]:
                  assert subprocess.check_output(['unsquashfs','-cat',str(a/'rootfs.squashfs'),rel])==expected
              print('Packaging, mutable logo and quoted paths checks passed.')
