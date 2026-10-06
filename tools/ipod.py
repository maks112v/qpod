"""Build-time edits for the SHA-pinned UI assets (local browsing, settings, status bar) and native call sites.

No global widget hook: excluded pages stay byte-identical; iPod's list style edits are audited in place.
The audit records full original instructions and asset hashes, not search/replace patterns.
"""
import copy
import functools
import hashlib
import json
import math
import pathlib
import re
import struct
from build import ROOT, check as require, fileoff

AUDIT = json.loads((pathlib.Path(__file__).resolve().parents[1]/'patch/ipod.json').read_text())
UI_ASSETS = AUDIT['assets'] | AUDIT['navbar_only'] | AUDIT['slide_only']
# The app window is the 375x320 screen minus the 30px status bar, so a 290px list holds four
# 72px rows. The stock 52px artwork is drawn 1:1 (no rescaling) with an 8px inset inside the
# 68px row body. Rows keep the row layout's eight-pixel left margin for the artwork.
BOTTOM = 290
PITCH = 72
BODY = PITCH - 4
ART = 52
ART_INSET = (BODY - ART) // 2
MARGIN = 8
# The 375x320 panel's glass has rounded corners that hide whatever is drawn in them. CORNER_R is
# the device calibration knob: the corner radius in pixels, fitted to device photos (V5.4I cut
# "20 of 29" at x 8, y 42 and the "Sy" of the last Home row, which needs about 80) and to stock's
# 50px status bar margins, which clear it. Text and icons near a corner keep CORNER_SLACK more.
CORNER_R = 80
CORNER_SLACK = 4


def corner_inset(y):
    """The width the glass hides at each end of screen row y."""
    d = min(max(CORNER_R - y, y - (320 - CORNER_R), 0), CORNER_R)
    return math.ceil(CORNER_R - math.sqrt(CORNER_R**2 - d*d))


def corner_x(y, h):
    """The side inset, slack included, for content spanning screen rows y to y + h."""
    return max(corner_inset(y), corner_inset(y + h)) + CORNER_SLACK


# iPod status bar (system_bar.bin, 375x30). Its 16px icons sit at y 7 to 23, so both groups keep
# clear of the top corners. The play state and EQ are on the left, as in stock; Bluetooth/codec,
# Wi-Fi and the battery on the right. The clock is centred on the screen, between the wider
# group's extent with every icon shown and the same distance from the other edge; CLOCK_MIN leaves
# room for its widest text, "12:59 PM" (86px at 20px in the pinned stock font). STATUS_PAD keeps the icons
# that much further in than the corners need, so they don't look cramped against the glass.
STATUS_PAD = 2
STATUS_MARGIN = corner_x(7, 16) + STATUS_PAD
CLOCK_MIN = 105
CLOCK_TEXT = 86
# iPod Home: seven HOME_ROW rows from HOME_TOP below the status bar, with room above and below. The
# labels fit the longest English one ("Playback Setting", 149px at 20px) and all start where the last
# row's clears the bottom-left corner. The art fills the right panel, edge to edge below the status
# bar; the payload fits it to each cover and crops it evenly (coverflow_home_art). In Full
# the rows end at HOME_FULL_ROW (patch/offsets.inc), so the chevron's glyph mirrors the text margin.
INC = (ROOT/'patch/offsets.inc').read_text()


# The integer #defines of patch/offsets.inc, which the payload compiles with.
O = {m[1]: int(m[2], 0) for m in re.finditer(r'^#define\s+(\w+)\s+(0x[0-9A-Fa-f]+|\d+)\b', INC, re.M)}
inc = O.__getitem__


CHEVRON_W = inc('CHEVRON_W')
# iPod page transition: the window anim_hint of the browsing, settings and equalizer pages. Stock's
# htranslate animator slides a page in from the right and back out on Return; the duration is the
# calibration knob.
SLIDE = 'htranslate(duration=120)'
HOME_TOP = 8
HOME_ROW = 39
HOME_TEXT_X = max(MARGIN, corner_x(30 + HOME_TOP + 6 * HOME_ROW + (HOME_ROW - 20) // 2, 20))
HOME_LABEL_END = CHEVRON_W - 10  # label end to the row's right edge: 10px before the glyph (x 20 of 50)
HOME_LIST_W = HOME_TEXT_X + 149 + HOME_LABEL_END
HOME_ART_RECT = [HOME_LIST_W, 0, 375 - HOME_LIST_W, 290]  # the whole right panel under the status bar
# iPod Now Playing (Rockbox iVideo): a 40px top row, the art band below it, then the progress bar
# with the times under its ends. Stock draws the 3x10 A-B markers at y 250, so the 8px bar sits on
# 251; their x follows NP_BAR through the np_bar_* immediates in ipod.json. The window starts
# at screen y 30; the top and bottom rows take their insets from the corners. The bar is a capsule
# (round_radius half its height) and the payload rounds the art's corners (paint_cover).
# The art and the metadata keep NP_MARGIN from the sides, 12px apart; the art is as large as that
# leaves while the text column keeps NP_TEXT_W, and "3 of 12" starts in line with the art.
NP_TOP = 40
NP_ICON = 50                     # the stock 50px control icons, centred in the top row
NP_MARGIN = 16
NP_POS_X = max(NP_MARGIN, corner_x(30 + (NP_TOP - 16) // 2, 16))  # "3 of 12", 16px text
NP_ICONS_END = 375 - corner_x(30, NP_TOP)  # the 50px icon images fill the row's height
NP_TEXT_W = 165
NP_TITLE_PX = 22                 # the title over the 16px artist and album, as Apple's hierarchy
NP_ART = 375 - 2 * NP_MARGIN - 12 - NP_TEXT_W
NP_SLIDE_H = 186                 # the swipeable art, lyrics and info pages; the dots sit below
NP_BAR_X = max(MARGIN, corner_x(30 + 251, 8))
NP_BAR = [NP_BAR_X, 251, 375 - 2 * NP_BAR_X, 8]
NP_TIMES_Y = NP_BAR[1] + NP_BAR[3] + 6  # 14px text in a 16px label, clear of the 10px A-B markers
NP_TIME_X = max(MARGIN, corner_x(30 + NP_TIMES_Y + 1, 14))
NP_TEXT_X = NP_MARGIN + NP_ART + 12
NP_GREY = '#AAAAAA'              # stock secondary text (s_scrlabel_gray24l)
# The track is TRACK_COLOR; the fill is Graphite's light tone until ringnav_playing sets the
# accent's (3.3:1 or more on the track for every preset).
NP_TRACK = f'#{inc("TRACK_COLOR"):06x}'
NP_FILL = '#' + re.search(r'#define ACCENTS \{ 0x\w+, 0x\w+, 0x(\w+),', INC)[1]


def decode(data):
    require(data[:4] == bytes.fromhex('12122211'), 'Unexpected AWTK UI magic')
    i = 4

    def string():
        nonlocal i
        end = data.index(0, i)
        value = data[i:end].decode('utf-8')
        i = end + 1
        return value

    def node():
        nonlocal i
        require(i + 48 <= len(data), 'Truncated UI widget')
        kind = data[i:i+32].split(b'\0')[0].decode('ascii')
        i += 32
        geometry = list(struct.unpack_from('<4i', data, i))
        i += 16
        props = {}
        while data[i]:
            key, value = string(), string()
            require(key not in props, 'Duplicate UI property')
            props[key] = value
        i += 1
        children = []
        while data[i]:
            children.append(node())
        i += 1
        return [kind, geometry, props, children]

    root = node()
    require(i == len(data), 'Trailing UI data')
    return root


def walk(n):
    yield n
    for child in n[3]:
        yield from walk(child)


def encode(root):
    def node(n):
        kind, geometry, props, children = n
        return (kind.encode().ljust(32, b'\0') + struct.pack('<4i', *geometry) +
                b''.join(k.encode()+b'\0'+v.encode()+b'\0' for k, v in props.items()) +
                b'\0' + b''.join(node(c) for c in children) + b'\0')
    return bytes.fromhex('12122211') + node(root)


# Both variants. Stock artist detail tabs carry literal Chinese `text` in every language; the
# stock string table already has these keys. The Albums tab and page start active (see ARTIST_ALBUMS).
ARTIST_PAGE = 'localmusic/artistinfo_page.bin'
ARTIST_TABS = {'btn_track': ('单曲', 'local_allsongs'), 'btn_album': ('专辑', 'album')}
# Stock init builds the Songs view (0x4adcbc); call the stock Albums tab click handler (0x4ac7ec)
# instead, which sets the tab state, queries the artist's albums and builds them. Songs stays a tap away.
ARTIST_ALBUMS = [(0x4aebd0, 0x2739dcbc, 0x2739c7ec), (0x4aebd4, 0x0411fc39, 0x0411f705)]


def artist_tabs(root):
    found = []
    for n in walk(root):
        name = n[2].get('name')
        if name in ARTIST_TABS:
            text, key = ARTIST_TABS[name]
            require(n[0] == 'tab_button' and n[2].get('text') == text, f'{name}: unexpected tab')
            n[2] = {('tr_text' if k == 'text' else k): (key if k == 'text' else v) for k, v in n[2].items()}
            if name == 'btn_album':
                n[2]['value'] = 'true'
            found.append(name)
        # tab_button loads before its pages sibling and can't sync it, so show the Albums view too.
        if n[0] == 'pages':
            require('value' not in n[2], 'Unexpected artist pages')
            n[2]['value'] = '1'
            found.append('pages')
    require(sorted(found) == sorted([*ARTIST_TABS, 'pages']), 'Unexpected artist tabs')


# Both variants. Coverflow's Home card: a clone of Local Music at index 2 with its own icon; its image
# (patch/coverflow.c binds it) is the click target. Stock translates label_* by name and ignores this one, so its text is literal.
# Local Music's label is literal too, "Library" in every language: stock would set label_localmusic's
# text to small_local ("Local Songs"), a key only Home uses.
HOME_PAGE = 'home_page.bin'
LIBRARY = {'name': 'label_library', 'text': 'Library'}


def home_card(root):
    menu = [n for n in root[3] if n[0] == 'slide_menu']
    require(len(menu) == 1, 'Unexpected home carousel')
    cards = [n[2].get('name') for n in menu[0][3]]
    require(cards[:2] == ['btn_playing', 'btn_localmusic'], 'Unexpected home cards')
    card = copy.deepcopy(menu[0][3][1])
    card[2]['name'] = 'btn_coverflow'
    image, label = card[3]
    require(image[2].get('name') == 'img_localmusic' and label[2].get('name') == 'label_localmusic',
            'Unexpected Local Music card')
    image[2]['name'] = 'img_coverflow'
    label[2]['name'] = 'label_coverflow'
    label[2]['text'] = 'Coverflow'
    for key, value in image[2].items():  # assets/icons/menu_coverflow*.png, added to the rootfs by build.py
        if key.endswith(':bg_image'): image[2][key] = value.replace('menu_music', 'menu_coverflow')
    menu[0][3].insert(2, card)
    menu[0][3][1][3][1][2].update(LIBRARY)


# iPod only. Home becomes a list of the stock cards' names, in stock order with Coverflow third.
# home_page_init (0x523c84) looks up no widget: its widget_foreach visitor (0x5239b4) binds img_*
# clicks and translates label_* by name, and only img_left/img_right, gone here, reach the
# slide_menu. Each row's transparent image covers the row, on top of its label, so it takes the
# tap and is the row's click target for the wheel.
HOME_ROWS = ['playing', 'localmusic', 'coverflow', 'folder', 'stream', 'playset', 'sysset']


# Stock list pages paint their list_view black inline; the theme default is a light rounded card.
LIST_BLACK = {f'style:{state}:{prop}': color for state in ('normal', 'disable', 'focused')
              for prop, color in (('bg_color', '#000000'), ('border_color', '#00000000'))}


def ipod_home(root):
    require([n[0] for n in root[3]] == ['slide_menu', 'image', 'image'], 'Unexpected home carousel')
    require([n[2]['name'] for n in root[3][0][3]] == ['btn_' + r for r in HOME_ROWS if r != 'coverflow'],
            'Unexpected home cards')
    rows = []
    for i, name in enumerate(HOME_ROWS):
        # Translations longer than English's longest end in an ellipsis before the chevron.
        label = {'name': 'label_' + name, 'style': 's_scrlabel_white20l', 'only_focus': 'true', 'ellipses': 'true'}
        if name == 'coverflow':
            label['text'] = 'Coverflow'
        if name == 'localmusic':
            label.update(LIBRARY)
        rows.append(['view', [0, i * HOME_ROW, HOME_LIST_W, HOME_ROW], {'name': 'btn_' + name}, [
            ['hscroll_label', [HOME_TEXT_X, 0, HOME_LIST_W - HOME_TEXT_X - HOME_LABEL_END, HOME_ROW], label, []],
            ['image', [0, 0, HOME_LIST_W, HOME_ROW], {'name': 'img_' + name, 'clickable': 'true'}, []]]])
    # The list_view's layout (0x5ea3a4) makes its scroll view vertical only for a mobile scroll bar,
    # which Home has none of, and scroll_view_create leaves it off; the payload navigates only
    # vertical scroll views.
    view = ['scroll_view', [0, 0, HOME_LIST_W, HOME_ROW * len(rows)],
            {'name': 'scroll_view_home', 'self_layout': 'default(x=0,y=0,w=100%,h=100%)', 'yslidable': 'true'}, rows]
    root[3] = [
        ['list_view', [0, HOME_TOP, HOME_LIST_W, HOME_ROW * len(rows)],
         {'name': 'list_view_home', 'item_height': str(HOME_ROW), **LIST_BLACK}, [view]],
        ['image', HOME_ART_RECT, {'name': 'img_homeart', 'image': 'default_album_big', 'draw_type': 'fill'}, []]]


def style_props(data):
    """Yield (widget, style, state, prop, value offset, value) for each property of an AWTK style file."""
    magic, _, count = struct.unpack_from('<3I', data)
    require(magic == 0xfafbfcfd, 'Unexpected AWTK style magic')
    for i in range(count):
        at, *names = struct.unpack_from('<I32s32s32s', data, 12 + 100*i)
        state, style, widget = (n.split(b'\0')[0].decode() for n in names)
        props, at = struct.unpack_from('<I', data, at)[0], at + 4
        for _ in range(props):
            _, key_len, size = struct.unpack_from('<BBH', data, at)
            prop, at = data[at+4:at+3+key_len].decode(), at + 4 + key_len
            yield widget, style, state, prop, at, data[at:at+size]
            at += size


def patch_style(data, audit):
    """Replace each edit's old value, same size, in the n states of widget/style that hold it."""
    require(hashlib.sha256(data).hexdigest() == audit['sha256'], 'Unaudited style file')
    out, props = bytearray(data), list(style_props(data))
    for widget, style, prop, old, new, n in audit['edits']:
        old, new = bytes.fromhex(old), bytes.fromhex(new)
        require(len(old) == len(new), f'{style}.{prop}: edit changes size')
        sites = [at for w, s, _, p, at, value in props if (w, s, p, value) == (widget, style, prop, old)]
        require(len(sites) == n, f'{style}.{prop}: expected {n} states with {old.hex()}, found {len(sites)}')
        for at in sites:
            out[at:at+len(new)] = new
    return bytes(out)


# iPod only. systembar_showface (0x52f610) finds every widget by name, recursively from the bar,
# and each second re-shows the volume, EQ, BT, synclink and Wi-Fi widgets and resets their text and
# images, but never their geometry. So widgets iPod hides move off-screen instead of going invisible.
# The volume number stays hidden: stock already opens dialog/volume_dialog on every wheel change.
STATUS_BAR = 'system_bar.bin'
STATUS_LEFT = ['img_state', 'label_eq']
STATUS_RIGHT = ['img_bt', 'img_wifi', 'label_battery', 'view_battery', 'img_battery']
STATUS_HIDDEN = ['img_vol', 'label_vol', 'img_synclink']
# The Battery setting (navigation.c bar_sync) shows one of the last three: the stock icon, stock's
# "88%" (label_battery at BATT_PCT_PX, the icons' height, and right-aligned so its width grows away
# from the corner; "100%" is BATT_PCT_W) or the payload's horizontal battery with the number inside (view_battery). The layout
# skips hidden children. The Bluetooth images are 42px canvases whose ink ends at column 41: BT_REACH
# is how far the plain glyph's ink starts left of img_bt's right edge. With it, Wi-Fi and the wider
# battery, the group's ink stays CLOCK_GAP clear of the widest clock text, CLOCK_TEXT ("12:59 PM"),
# so every mode fits once a codec badge has faded; wider badges fall back to the icon (BATT_ROOM).
BATT_MODES = STATUS_RIGHT[2:4]
BATT_PCT_W, BATT_PCT_PX, BATT_ROOM, BT_REACH = (inc(n) for n in ('BATT_PCT_W', 'BATT_PCT_PX', 'BATT_ROOM', 'BT_REACH'))
BATT_H_W = inc('BATT_BODY_W') + inc('BATT_NUB_W')  # the payload's battery with its nub
CLOCK_GAP = 4


def status_bar(root):
    left, right = root[3]
    require([left[2].get('name'), right[2].get('name')] == ['view_left', 'view_right'], 'Unexpected status bar')
    widgets = {n[2]['name']: n for n in left[3] + right[3]}
    require(sorted([*widgets, 'view_battery']) == sorted(STATUS_LEFT + STATUS_RIGHT + STATUS_HIDDEN), 'Unexpected status bar widgets')
    pct = widgets['label_battery']
    pct[1][2] = BATT_PCT_W
    for s in ('normal', 'disable', 'focused'):
        require(pct[2][f'style:{s}:font_size'] == '18', 'Unexpected battery label')
        pct[2].update({f'style:{s}:font_size': str(BATT_PCT_PX), f'style:{s}:text_align_h': 'right'})
    pct[2]['visible'] = 'false'
    widgets['view_battery'] = ['view', [0, 0, BATT_H_W, 0], {'name': 'view_battery', 'visible': 'false'}, []]
    left[3] = [widgets[n] for n in STATUS_LEFT]
    right[3] = [widgets[n] for n in STATUS_RIGHT]
    for view in (left, right):
        layout = view[2]['children_layout']
        require('xm=50,s=5)' in layout, 'Unexpected status bar layout')
        view[2]['children_layout'] = layout.replace('xm=50', f'xm={STATUS_MARGIN}')
    shown = [[n for n in v[3] if n[2]['name'] not in BATT_MODES] for v in (left, right)]
    extent = max(STATUS_MARGIN + sum(n[1][2] for n in v) + 5 * (len(v) - 1) for v in shown)
    width = 375 - 2 * extent
    require(width >= CLOCK_MIN and right[1][0] + right[1][2] == 375, f'Status bar clock {width}px, too narrow')
    room = int(375 - STATUS_MARGIN - (375 / 2 + CLOCK_TEXT / 2 + CLOCK_GAP))
    reach = BT_REACH + 5 + widgets['img_wifi'][1][2] + 5 + max(BATT_PCT_W, BATT_H_W)
    require(BATT_ROOM == room and reach <= room, f'Status bar battery needs {reach}px of {room}px')
    for name in STATUS_HIDDEN:
        g = widgets[name][1]
        g[0], g[3] = -200, 30  # still updated by stock, drawn off-screen
        root[3].append(widgets[name])
    # The payload writes the local time here (ringnav_paint_bg).
    root[3].append(['hscroll_label', [extent, 0, width, 30], {
        'name': 'label_clock', 'style': 's_scrlabel_white20c', 'only_focus': 'true'}, []])


# iPod only. Stock finds every Now Playing widget by name, recursively, so they can move: title, artist
# and a new album label join the art on the slide_view's first page, so a swipe still swaps all of it for
# the lyrics or info page. Those keep their stock 225px column (stock creates 225px lyric lines),
# centred. The payload fills the label_ipod_* labels (ringnav_playing); label_playlen, the total, hides.
PLAYING_PAGE = 'playing_page.bin'


def plain_slider(props, track, fill, bar):
    """A slider's props drawn in plain colour (stock slider paint uses bg/fg_color without images). The
    style has no theme entry, so no thumb icon either: stock then fills exactly to the value, and
    slide_with_bar keeps tap and drag."""
    props = {k: v for k, v in props.items() if not k.endswith((':bg_image', ':fg_image', ':icon', ':y_offset'))}
    for key in props:
        if key.endswith(':bg_color'): props[key] = track
        if key.endswith(':fg_color'): props[key] = fill
    props.update(style='s_ipod_progress', bar_size=str(bar))
    return props


def playing_page(root):
    named = {n[2].get('name'): n for n in walk(root)}
    require([n[2].get('name') for n in root[3]] == [
        'view_buttons', 'scrlabel_title', 'scrlabel_artist', 'label_playtime', 'label_playlen',
        'slide_view_view', 'slider_play', 'img_repeata', 'img_repeatb', 'image_wait'], 'Unexpected Now Playing page')
    buttons, title, artist = root[3][:3]
    buttons[1] = [0, 0, 375, NP_TOP]
    named['img_return'][1][0] = -200  # the hardware Return, as on the pages whose navbars are hidden
    # Stock still updates this widget by name; mode selection lives in the bottom control now.
    named['img_playmode'][1][0] = -200
    named['img_playmode'][2].update(visible='false', enable='false')
    for i, name in enumerate(['img_fav', 'img_more']):
        n = named[name]
        n[1] = [NP_ICONS_END - (2 - i) * NP_ICON, 0, NP_ICON, NP_TOP]
        n[2] = {k: v for k, v in n[2].items() if not k.endswith(('_offset', 'text_align_h'))}
        if 'image' in n[2]:
            n[2]['draw_type'] = 'center'
    buttons[3].append(['label', [NP_POS_X, 0, NP_ICONS_END - 2 * NP_ICON - NP_POS_X, NP_TOP], {
        'name': 'label_ipod_pos', 'style:normal:font_size': '16', 'style:normal:text_color': NP_GREY,
        'style:normal:text_align_h': 'left'}, []])

    art_y = (NP_SLIDE_H - NP_ART) // 2
    text_w = 375 - NP_MARGIN - NP_TEXT_X
    top = art_y + NP_ART // 2 - (28 + 4 + 20 + 4 + 20) // 2  # the three lines centre on the art
    title[1] = [NP_TEXT_X, top, text_w, 28]
    title[2].update({'style': 's_scrlabel_white20l', 'style:normal:font_size': str(NP_TITLE_PX)})
    artist[1] = [NP_TEXT_X, top + 32, text_w, 20]
    for key in artist[2]:
        if key.endswith(':text_color'): artist[2][key] = NP_GREY
        if key.endswith(':text_align_h'): artist[2][key] = 'left'
    album = copy.deepcopy(artist)
    album[1] = [NP_TEXT_X, top + 56, text_w, 20]
    album[2].update(name='label_ipod_album', text='')
    named['img_cover'][1] = [NP_MARGIN, art_y, NP_ART, NP_ART]
    named['img_playstate'][1] = [NP_MARGIN + (NP_ART - 120) // 2, art_y + (NP_ART - 120) // 2, 120, 120]
    named['view_album'][3] += [title, artist, album]
    column = (375 - 225) // 2
    named['label_lyricmsg'][1][0] += column
    named['view_lrc'][2]['self_layout'] = f'default(x={column},y=0,w=225,h=178)'
    for n in named['view_info'][3]:
        n[1][0] += column
    named['slide_view_view'][1] = [0, NP_TOP, 375, NP_SLIDE_H + 12]
    named['slide_view'][1] = [0, 0, 375, NP_SLIDE_H]
    dots = named['slide_indicator1']
    dots[1][1] = NP_SLIDE_H + 2
    dots[2]['self_layout'] = f'default(x=0,y={NP_SLIDE_H + 2},w=100%,h=10)'
    named['image_wait'][1] = [NP_MARGIN + (NP_ART - 54) // 2, NP_TOP + art_y + (NP_ART - 54) // 2, 54, 54]

    slider = named['slider_play']
    x, y, w, h = NP_BAR
    slider[1] = [x, y - 11, w, h + 22]
    slider[2] = plain_slider(slider[2], NP_TRACK, NP_FILL, h)
    require(h // 2 > 3, 'Stock squares a slider radius of 3 or less')
    for key in slider[2]:
        if key.endswith(':round_radius'): slider[2][key] = str(h // 2)  # a capsule, track and fill
    for name in ('img_repeata', 'img_repeatb'):
        named[name][1][0] = x
    total = named['label_playlen']
    remain = copy.deepcopy(total)
    remain[1] = [375 - NP_TIME_X - 80, NP_TIMES_Y, 80, 16]
    remain[2].update(name='label_ipod_remain', text='')
    total[2]['visible'] = 'false'
    named['label_playtime'][1] = [NP_TIME_X, NP_TIMES_Y, 80, 16]
    for n in (named['label_playtime'], remain):
        for key in n[2]:
            if key.endswith(':text_color'): n[2][key] = NP_GREY
    root[3].append(['label', [x, y - 11, w, h + 22], {
        'name': 'label_ipod_control', 'visible': 'false',
        'style:normal:font_size': '20', 'style:normal:text_color': '#ffffff',
        'style:normal:text_align_h': 'center', 'style:normal:text_align_v': 'middle'}, []])
    root[3][1:3] = []
    root[3].insert(3, remain)


# iPod only. Quick settings (the pull-down statusbar_dialog, which covers the whole screen): the eight
# stock 60px controls stay in their four columns, two rows from QS_TOP. Each label gets the same
# two-line QS_LABEL_H area in the stock 16px style, top-aligned so single- and two-line labels start
# on one line, QS_LABEL_GAP under its icon and QS_ROW_GAP above the next row. Brightness becomes a
# slim QS_BAR track in a QS_TOUCH-high slider (tap or drag anywhere on it, as stock) between the
# stock dim and bright suns, which keep their images. dialog_statusbar_dialog_init (0x4a0d88)
# finds these widgets by name and never moves or resizes them.
QUICK_SETTINGS = 'dialog/statusbar_dialog.bin'
QS_TOP, QS_ICON, QS_LABEL_GAP, QS_LABEL_H, QS_ROW_GAP, QS_LABEL_W = 20, 60, 6, 40, 12, 80
QS_PITCH = QS_ICON + QS_LABEL_GAP + QS_LABEL_H + QS_ROW_GAP
QS_SUN, QS_BAR, QS_TOUCH = 26, 6, 48
QS_EDGE = 30                     # the suns line up with the first and last icon columns
QS_TRACK = '#3A3A3A'  # a grey that reads on the black dialog
QS_GRID = {'wifiswitch': 'wifi', 'btswitch': 'bt', 'lock': 'lock', 'gain': 'gain',
           'usbmode': 'usbmode', 'po': 'outputway', 'playset': 'playset', 'sysset': 'sysset'}


def quick_settings(root):
    menu, light = root[3]
    require([menu[2].get('name'), light[2].get('name')] == ['view_menu', 'view_backlight'], 'Unexpected quick settings')
    named = {n[2]['name']: n for n in menu[3]}
    require(sorted(named) == sorted([f'img_{k}' for k in QS_GRID] + [f'label_{v}' for v in QS_GRID.values()]),
            'Unexpected quick settings controls')
    for i, (icon, label) in enumerate(QS_GRID.items()):
        image, text = named['img_' + icon], named['label_' + label]
        x, y, w, h = image[1]
        require((w, h) == (QS_ICON, QS_ICON) and y == (0, 112)[i // 4] and text[2].get('style') == 's_label_white18c',
                f'{icon}: unexpected quick settings control')
        image[1][1] = (i // 4) * QS_PITCH
        text[1] = [x + (QS_ICON - QS_LABEL_W) // 2, image[1][1] + QS_ICON + QS_LABEL_GAP, QS_LABEL_W, QS_LABEL_H]
        text[2].update({'style': 's_label_white16c', 'style:normal:text_align_v': 'top'})
    menu[1] = [0, QS_TOP, 375, 2 * QS_PITCH - QS_ROW_GAP]
    slider, dim, bright = light[3]
    require([n[2]['name'] for n in light[3]] == ['slider_backlight', 'image0', 'image1'] and
            (dim[1][2], bright[1][2]) == (QS_SUN, QS_SUN), 'Unexpected brightness row')
    light[1] = [0, menu[1][1] + menu[1][3] + QS_ROW_GAP, 375, QS_TOUCH]
    require(light[1][1::2] == [inc('VOL_PANEL_Y'), inc('VOL_PANEL_H')], 'The volume panel must cover the brightness row')
    dim[1] = [QS_EDGE, (QS_TOUCH - QS_SUN) // 2, QS_SUN, QS_SUN]
    bright[1] = [375 - QS_EDGE - QS_SUN, (QS_TOUCH - QS_SUN) // 2, QS_SUN, QS_SUN]
    track = [QS_EDGE + QS_SUN + 12, 0, 375 - 2 * (QS_EDGE + QS_SUN + 12), QS_TOUCH]
    require(slider[2].get('slide_with_bar') == 'true', 'Unexpected brightness slider')
    props = plain_slider(slider[2], QS_TRACK, '#FFFFFF', QS_BAR)
    props.update(self_layout='default(x={},y=0,w={},h={})'.format(*track[::2], QS_TOUCH), dragger_size=str(QS_TOUCH))
    slider[1], slider[2] = track, props


# iPod only. The confirm pair (img_cancel, img_enter; 80px tiles around 60px discs) sits symmetrically,
# each centred in its half of the screen. The discs themselves are recoloured dark with legible
# glyphs for every accent by ringnav_image_add (patch/navigation.c).
CONFIRM = 'dialog/confirminfo_dialog.bin'
CONFIRM_TILE = 80


def confirm_dialog(root):
    require([n[2].get('name') for n in root[3]] == ['img_cancel', 'img_enter'] and
            all(n[1][2:] == [CONFIRM_TILE, CONFIRM_TILE] for n in root[3]), 'Unexpected confirm dialog')
    x = (375 // 2 - CONFIRM_TILE) // 2
    root[3][0][1][0], root[3][1][1][0] = x, 375 - x - CONFIRM_TILE


# iPod only. The volume dialog loses its highlight="default(alpha=200)", so the window manager
# creates no highlighter and nothing under it dims; the payload draws the volume into the dialog
# (navigation.c vol_paint): a band on Now Playing, a panel elsewhere.
VOLUME = 'dialog/volume_dialog.bin'


def volume_dialog(root):
    require(root[0] == 'dialog' and root[2].get('name') == 'volume_dialog' and
            root[2].get('highlight') == 'default(alpha=200)', 'Unexpected volume dialog')
    del root[2]['highlight']


# iPod only. Settings and Streaming lose their navbar, as on the local pages, and their lists hold
# SET_ROWS complete SET_ROW rows from SET_TOP; ipod_list_layout (patch/navigation.c) lays the native
# rows out to match. Tidal keeps its navbars: most hold a search button with no hardware equivalent.
NAVBAR_ONLY = AUDIT['navbar_only']
SET_ROW, SET_TOP, SET_ROWS, SET_STOCK_ROW = (inc(n) for n in ('SET_ROW', 'SET_TOP', 'SET_ROWS', 'SET_STOCK_ROW'))

# iPod only. The settings rows' stock 52px artwork (SET_STOCK_ICON), pinned by hash in ipod.json,
# is pre-sized to SET_ICON at build time so the rows draw it 1:1 instead of scaling it on the device.
# ImageMagick's Lanczos resize weights colour by alpha, so edges keep their colour and transparency;
# -strip and the excluded date chunks keep the bytes reproducible. Only native settings code names
# these images, so no other screen sees the smaller size.
SETTINGS_ICONS = AUDIT['settings_icons']
SET_ICON, SET_STOCK_ICON = inc('SET_ICON'), inc('SET_STOCK_ICON')


def png_header(data):
    """(width, height, bit depth, colour type) from a PNG's IHDR."""
    require(data[:8] == b'\x89PNG\r\n\x1a\n' and data[12:16] == b'IHDR', 'Not a PNG')
    return struct.unpack('>IIBB', data[16:26])


def imagemagick(*args, data):
    import shutil, subprocess
    tool = shutil.which('magick') or shutil.which('convert')
    require(tool is not None, 'ImageMagick (magick or convert) is required for the iPod settings icons')
    return subprocess.run([tool, *args], input=data, stdout=subprocess.PIPE, check=True).stdout


def settings_icon(name, data):
    require(hashlib.sha256(data).hexdigest() == SETTINGS_ICONS.get(name), f'{name}: unaudited settings icon')
    require(png_header(data) == (SET_STOCK_ICON, SET_STOCK_ICON, 8, 6), f'{name}: unexpected stock icon format')
    out = imagemagick('png:-', '-alpha', 'on', '-filter', 'Lanczos', '-resize', f'{SET_ICON}x{SET_ICON}!',
                      '-strip', '-define', 'png:exclude-chunks=date,time', '-define', 'png:color-type=6',
                      '-define', 'png:bit-depth=8', 'png:-', data=data)
    require(png_header(out) == (SET_ICON, SET_ICON, 8, 6), f'{name}: filtered icon is not {SET_ICON}px RGBA')
    opaque = [imagemagick('png:-', '-format', '%[opaque]', 'info:', data=d) for d in (data, out)]
    require(opaque[0] == opaque[1], f'{name}: filtering changed the transparency')
    return out


def patch_word(data, address, old, new, purpose, changes=None):
    off = fileoff(data, address)
    require(struct.unpack_from('<I', data, off)[0] == old, f'{address:#x}: unexpected instruction')
    struct.pack_into('<I', data, off, new)
    if changes is not None:
        changes.append(dict(address=hex(address), original=hex(old), patched=hex(new), purpose=purpose))


def patch_asset(path, data, ipod):
    require(hashlib.sha256(data).hexdigest() == UI_ASSETS[path], f'{path}: unaudited UI asset')
    root = decode(data)
    require(encode(root) == data, f'{path}: UI round trip differs')
    if path == ARTIST_PAGE:
        artist_tabs(root)
    whole = ({HOME_PAGE: ipod_home, STATUS_BAR: status_bar, PLAYING_PAGE: playing_page, QUICK_SETTINGS: quick_settings,
              CONFIRM: confirm_dialog, VOLUME: volume_dialog} if ipod else {HOME_PAGE: home_card})
    if path in whole:
        whole[path](root)
    if path in whole or not ipod:
        return encode(root)
    # Home is never reopened; Now Playing, Coverflow and the dialogs open at once.
    require(root[0] == 'window' and not any(k.endswith('anim_hint') for k in root[2]), f'{path}: unexpected window')
    root[2]['anim_hint'] = SLIDE
    if path in AUDIT['slide_only']:  # the PEQ editor builds its own children (patch/peq_ui.c)
        return encode(root)
    nav = [n for n in root[3] if n[2].get('name') == 'view_navbar']
    require(len(nav) == 1 and nav[0][1] in ([0, 0, 375, 50], [0, 0, 370, 50]), f'{path}: unexpected toolbar')  # 370: stream_page
    # Keep the widget (and callback lookups) alive. Children may be recreated by stock.
    nav[0][2]['visible'] = 'false'
    nav[0][2]['enable'] = 'false'
    for n in root[3]:
        if n is nav[0]:
            continue
        kind, g, props, _ = n
        if kind == 'list_view' and path in NAVBAR_ONLY:  # settings rows: see ipod_list_layout
            require(g[1] == 50 and props.get('default_item_height') == str(SET_STOCK_ROW) and
                    'item_height' not in props, f'{path}: unexpected settings list')
            props['default_item_height'] = str(SET_ROW)
            g[1], g[3] = SET_TOP, SET_ROWS * SET_ROW
        elif kind in ('list_view', 'table_view', 'tab_control'):
            require(g[1] in (50, 100), f'{path}: unexpected list position')
            g[1] -= 50
            g[3] = BOTTOM - g[1]
        elif props.get('name') == 'view_navbar_allplay':
            require(g == [0, 50, 375, 50], f'{path}: unexpected action bar')
            g[1] = 0
        elif kind == 'list_item':  # playlist editing actions
            g[1] = 10 + ((g[1] - 60) // 78) * PITCH
        elif props.get('name') == 'view':  # allmusic's stock empty-state panel
            g[1] -= 50
        elif path in NAVBAR_ONLY:  # settings panels and notes keep their size; text ends clear of a top corner
            require(g[1] >= 50, f'{path}: unexpected content under the toolbar')
            g[1] -= 50
            if kind in ('label', 'hscroll_label') and corner_inset(30 + g[1]):
                g[2] = min(g[2], 375 - g[0] - corner_x(30 + g[1], 0))
    if path in NAVBAR_ONLY:
        return encode(root)

    def rows(n, in_row=False):
        kind, g, props, children = n
        if path == 'localmusic/album_page.bin' and props.get('name') == 'button1':  # inline black grid buttons
            for key, value in props.items():
                if key.endswith(':bg_color') and value == '#000000':
                    props[key] = '#00000000'
        in_row = in_row or kind in ('list_item', 'table_row') and g[3] in (0, 78)
        for prop in ('row_height', 'default_item_height'):
            if props.get(prop) == '78':
                props[prop] = str(PITCH)
        if in_row:
            if kind in ('list_item', 'table_row') and g[3] == 78:
                g[3] = PITCH
            elif g[3] == 70:
                g[3] = BODY
            if g[1] in (11, 14, 21, 40, 41):
                # titles and metadata keep their stock centring: the body shrank by two pixels
                g[1] -= 1
            elif g[1] in (9, 10) and kind == 'image':
                g[1] = ART_INSET
        if path == ARTIST_PAGE:
            if kind == 'pages':
                require(props.get('self_layout') == 'default(x=0,y=40,w=100%,h=170)', 'Unexpected tabs layout')
                props['self_layout'] = f'default(x=0,y=40,w=100%,h={BOTTOM - 40})'
                g[3] = BOTTOM - 40
            if props.get('name') == 'list_view_album':
                g[3] = BOTTOM - 40
        for child in children:
            rows(child, in_row)
    rows(root)
    return encode(root)


def patch_code(data, symbols):
    changes = []
    word = functools.partial(patch_word, data, changes=changes)

    for group in AUDIT['immediates']:
        value = {'pitch': PITCH, 'body': BODY, 'art': ART, 'art_inset': ART_INSET,
                 'scroll': BOTTOM - 50, 'np_bar_x': NP_BAR[0], 'np_bar_w': NP_BAR[2]}.get(group['value'], group['value'])
        for address, instruction in group['sites']:
            old = int(instruction, 16)
            word(int(address, 16), old, (old & 0xffff0000) | value, group['purpose'])
    group = AUDIT['row_layout_calls']
    for address, instruction in group['sites']:
        word(int(address, 16), int(instruction, 16),
             0x0c000000 | (symbols['compact_set_row_layout'] >> 2), group['purpose'])
    word(0x522410, 0x0320f809, 0, 'folder rebind retains the width owned by its row layouter')
    # Only the final long-Return call changes. All stock gates and its release guard precede it.
    word(0x4e8924, 0x04110fdf, 0x0c000000 | (symbols['compact_now_playing'] >> 2),
         'long Return destination after stock input gates')
    # iPod settings rows: the list_view layouter's layout slot (data, vtable 0x926928 + 8).
    word(inc('LIST_VIEW_LAYOUT_SLOT'), inc('LIST_VIEW_LAYOUT'), symbols['ipod_list_layout'],
         'settings lists stack SET_ROW rows and map their children')
    # home_page_init's memory-play resume opens Now Playing; outside car mode it runs the page's player_start alone.
    word(0x523de0, 0x0320f809, 0x0c000000 | (symbols['ringnav_boot'] >> 2),
         'boot resume restores the queue paused and stays on Home')
    return changes
