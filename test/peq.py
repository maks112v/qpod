#!/usr/bin/env python3
"""Run the actual C parser/storage and PCM DSP checks."""
import cmath
import ctypes as C
import math
import pathlib
import subprocess
import tempfile
import sys; sys.path.insert(0, sys.path[0] + '/../tools')  # tools/ first: test/build.py must import tools/build.py
from build import FUNCTIONS
from peq import LIBC

PROTOTYPES = {**FUNCTIONS, **LIBC}  # the stock calls the host shims stand in for

ROOT = pathlib.Path(__file__).resolve().parents[1]

class Band(C.Structure):
    _fields_ = [('enabled', C.c_int), ('type', C.c_int), ('frequency', C.c_double),
                ('gain', C.c_double), ('q', C.c_double)]

class Preset(C.Structure):
    _fields_ = [('count', C.c_int), ('bypass', C.c_int), ('preamp', C.c_double), ('bands', Band * 30),
                ('balance', C.c_double)]

class Error(C.Structure):
    _fields_ = [('line', C.c_uint), ('reason', C.c_char_p)]

class Coeff(C.Structure):
    _fields_ = [(k, C.c_double) for k in ('b0', 'b1', 'b2', 'a1', 'a2')]

class Engine(C.Structure):
    _fields_ = [('c', Coeff * 30), ('z', ((C.c_double * 2) * 30) * 8),
                ('gain', C.c_double * 8), ('bypass', C.c_int), ('used', C.c_int), ('only', C.c_int * 30)]

class DSP(C.Structure):
    _fields_ = [('current', Engine), ('next', Engine), ('pending', Engine),
                *[(k, C.c_int) for k in ('rate', 'channels', 'ramp', 'ramp_length', 'waiting')]]

def compile_host(tmp, name, *sources):
    path = tmp/name
    subprocess.run(['cc', '-DPEQ_HOST', f'-DPEQ_ROOT="{tmp}/root"', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-shared', '-fPIC', *map(str, sources), '-lm', '-o', str(path)], check=True)
    return C.CDLL(str(path))

def library(tmp):
    lib = compile_host(tmp, 'peq.so', ROOT/'patch/peq.c')
    signatures = {
        'peq_default': [C.POINTER(Preset)], 'peq_valid': [C.POINTER(Preset)],
        'peq_parse': [C.c_char_p, C.c_uint, C.POINTER(Preset), C.POINTER(Error)],
        'peq_import_file': [C.c_char_p, C.POINTER(Preset), C.POINTER(Error)],
        'peq_save': [C.c_char_p, C.POINTER(Preset), C.c_int],
        'peq_load': [C.c_char_p, C.POINTER(Preset)],
        'peq_compile': [C.POINTER(Preset), C.c_int, C.POINTER(Engine)],
        'peq_reset': [C.POINTER(DSP), C.c_int, C.c_int, C.POINTER(Preset)],
        'peq_update': [C.POINTER(DSP), C.POINTER(Preset)],
        'peq_process': [C.POINTER(DSP), C.POINTER(C.c_float), C.c_uint],
    }
    for name, args in signatures.items(): getattr(lib, name).argtypes = args
    return lib

def parse(lib, text):
    data = text.encode() if isinstance(text, str) else text
    p, error = Preset(), Error()
    assert lib.peq_parse(data, len(data), C.byref(p), C.byref(error)), (error.line, error.reason, data)
    return p

def parser_check(lib, tmp):
    source = '\ufeff # comment\r\nPreamp : -3 dB # inline\r\nPreamp:\t-2 dB\n'
    aliases = ['PK', 'PEQ', 'LS', 'LSC', 'HS', 'HSC']
    for i, alias in enumerate(aliases):
        source += f'Filter {99-i}: {"ON" if i%2 else "OFF"}\t{alias} Fc {100+i*100} Hz Gain -6 dB'
        source += ' Q 1.3\n' if i < 2 else '\n'
    p = parse(lib, source)
    assert p.count == 6 and p.preamp == -5 and p.bypass == 0
    assert [b.type for b in p.bands[:6]] == [0, 0, 1, 1, 2, 2]
    assert [b.enabled for b in p.bands[:6]] == [0, 1, 0, 1, 0, 1]
    assert [b.frequency for b in p.bands[:6]] == [100, 200, 300, 400, 500, 600]
    a = 10 ** (-6 / 40)  # APO: shelf without Q is slope 0.9 at Fc
    assert abs(p.bands[2].q - 1 / math.sqrt((a + 1/a) * (1/0.9 - 1) + 2)) < 1e-15
    # APO: LS/HS with Q give a corner frequency (S=1 at Q 0.7071 -> shift 10^(gain/80)); LSC/HSC the centre.
    shelves = parse(lib, 'Filter: ON LS Fc 100 Hz Gain 12 dB Q 0.7071067811865476\n'
                         'Filter: ON HS Fc 8000 Hz Gain -12 dB Q 0.7071067811865476\n'
                         'Filter: ON LSC Fc 100 Hz Gain 12 dB Q 0.7\n'
                         'Filter 11: OFF None\nFilter: ON PK Fc 1000,5 Hz Gain -3,5 dB Q 1,41\n')
    assert abs(shelves.bands[0].frequency - 100 * 10**(12/80)) < 1e-9
    assert abs(shelves.bands[1].frequency - 8000 / 10**(12/80)) < 1e-9
    assert shelves.bands[2].frequency == 100 and shelves.count == 4
    assert (shelves.bands[3].frequency, shelves.bands[3].gain, shelves.bands[3].q) == (1000.5, -3.5, 1.41)
    edges = parse(lib, 'Filter: ON LS Fc 19000 Hz Gain 12 dB Q 0.7\nFilter: ON HS Fc 21 Hz Gain -12 dB Q 0.7\n')
    assert (edges.bands[0].frequency, edges.bands[1].frequency) == (20000, 20)
    single = 'Filter: ON PK Fc 1e3 Hz Gain +6.0 dB Q .7\n'
    assert parse(lib, single).preamp == 0
    assert parse(lib, 'Preamp: 24 dB\nPreamp: 24 dB\nPreamp: -48 dB').preamp == 0
    assert parse(lib, single * 30).count == 30
    assert parse(lib, single + 'Filter 2: OFF PK Fc 0 Hz Gain 0.0 dB Q 0.000').count == 1  # blank slot
    assert parse(lib, '').count == 0
    # Import is pure. Saving succeeds only after validation and explicit collision confirmation.
    active, saved = tmp/'active', tmp/'preset'
    for path in (active, saved): assert lib.peq_save(bytes(path), C.byref(p), 0) == 1
    baseline = bytes(p)
    failures = [
        (single * 30 + single.replace('ON', 'OFF'), 31, '30 bands'),
        ('Preamp: 24 dB\nPreamp: 1 dB', 2, 'summed'),
        ('Preamp: -61 dB', 1, 'preamp'),
        ('Preamp: -3', 1, 'expected'),
        ('\nGraphicEQ: 20 0; 20000 0', 2, 'unsupported'),
        ('Include: other.txt', 1, 'unsupported'), ('Channel: C', 1, 'only channels'), ('Channel:', 1, 'expected Channel'),
        ('Channel: L\nPreamp: -13 dB', 2, 'differ'),
        ('Eval: x=1', 1, 'unsupported'), ('garbage', 1, 'unsupported'),
        (single.replace(' Q .7', ''), 1, 'requires Q'),
        (single.replace('PK', 'HP'), 1, 'type'),
        (single.replace('PK', 'LS 6dB'), 1, 'form'),
        (single.replace('Q .7', 'BW Oct 1'), 1, 'form'),
        (single.replace('1e3', '`1000`'), 1, 'number'),
        (single.replace('Filter:', 'Filter x:'), 1, 'label'),
        (single + 'Filter: ON PK Fc 19 Hz Gain 0 dB Q 1', 2, 'range'),
        (b'\0', 1, 'NUL'), (b'#' * 513, 1, 'line'), (b'#' * 16385, 1, 'file'),
    ]
    for old, values in [('1e3', ['nan', 'inf', '-inf', '1e999', '20junk', '0x100', '20001', '-20']),
                        ('+6.0', ['nan', '25', '-25']), ('.7', ['NaN', '0', '-1', '10.1'])]:
        failures += [(single.replace(old, value), 1, 'number' if value in ('nan', 'inf', '-inf', '1e999', '20junk', '0x100', 'NaN') else 'range') for value in values]
    for text, line, reason in failures:
        data = text.encode() if isinstance(text, str) else text
        error = Error()
        assert not lib.peq_parse(data, len(data), C.byref(p), C.byref(error)), data
        assert error.line == line and reason in error.reason.decode(), (data, error.line, error.reason)
        assert bytes(p) == baseline
        assert active.read_bytes() == saved.read_bytes()
    q = parse(lib, single)
    before = saved.read_bytes()
    assert lib.peq_save(bytes(saved), C.byref(q), 0) == 2
    assert saved.read_bytes() == before  # cancel
    assert lib.peq_save(bytes(saved), C.byref(q), 1) == 1  # confirm
    assert active.read_bytes() == before  # never auto-activate
    source_file = tmp/'input.txt'
    source_file.write_text(single)
    assert lib.peq_import_file(bytes(source_file), C.byref(p), C.byref(Error()))
    source_file.unlink()
    assert lib.peq_load(bytes(saved), C.byref(p)) and bytes(p) == bytes(q)
    unchanged = bytes(p)
    for path in (source_file, tmp):
        e = Error()
        assert not lib.peq_import_file(bytes(path), C.byref(p), C.byref(e))
        assert e.line and e.reason and bytes(p) == unchanged
    saved.write_bytes(saved.read_bytes()[:-1])
    assert not lib.peq_load(bytes(saved), C.byref(p)) and bytes(p) == unchanged
    # APO Channel scopes: bands follow it, per-channel preamps become preamp + balance.
    scoped = parse(lib, 'Preamp: -3 dB\nChannel: L\nPreamp: -1.5 dB\n' + single + 'Channel: R\nPreamp: -0.5 dB\n'
                        + single + 'Channel: L R\n' + single + 'Channel: all\n' + single.replace('ON', 'OFF'))
    assert [b.enabled for b in scoped.bands[:4]] == [2, 3, 1, 0]
    assert parse(lib, single * 30 + 'Filter: OFF PK Fc 0 Hz Gain 0 dB Q 0').count == 30  # blank slots do not count
    assert scoped.preamp == -3.5 and scoped.balance == 1  # left 1 dB below right
    # v1 files (no balance) still load, with the balance centred.
    v1 = tmp/'v1'
    v1.write_bytes(b'Q2PEQ01\0' + bytes(q)[:16 + 10*32])
    assert lib.peq_load(bytes(v1), C.byref(p)) and bytes(p) == bytes(q)  # q's bands 11-30 are the defaults
    v1.write_bytes(b'Q2PEQ02\0' + bytes(q)[:-8])
    assert not lib.peq_load(bytes(v1), C.byref(p))
    v1.write_bytes(b'')
    assert not lib.peq_load(bytes(v1), C.byref(p))
    assert not lib.peq_save(bytes(tmp/'missing'/'preset'), C.byref(q), 1)
    print('PEQ parser/storage: syntax, limits, transaction failures, confirmation and persistence passed.')

def response(engine, rate, frequency, ch=0):
    if engine.bypass: return 1
    z = cmath.exp(-2j * math.pi * frequency / rate)
    result = complex(engine.gain[ch])
    for c, only in zip(engine.c, engine.only):
        if not only or only == ch + 1: result *= (c.b0 + c.b1*z + c.b2*z*z)/(1 + c.a1*z + c.a2*z*z)
    return abs(result)

def process(lib, d, values, channels=1):
    audio = (C.c_float * len(values))(*values)
    lib.peq_process(C.byref(d), audio, len(values)//channels)
    return list(audio)

def dsp_check(lib):
    p = parse(lib, 'Preamp: -6 dB\nFilter: ON PK Fc 1000 Hz Gain 6 dB Q 1')
    # The manual and imported representation use exactly the same compiler.
    manual = Preset(); lib.peq_default(C.byref(manual))
    manual.count, manual.bypass, manual.preamp = 1, 0, -6
    manual.bands[0] = Band(1, 0, 1000, 6, 1)
    for rate in (8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000):
        a, b = Engine(), Engine()
        assert lib.peq_compile(C.byref(p), rate, C.byref(a))
        assert lib.peq_compile(C.byref(manual), rate, C.byref(b))
        assert bytes(a) == bytes(b)
        assert abs(20 * math.log10(response(a, rate, 1000))) < 1e-7
        d = DSP(); lib.peq_reset(C.byref(d), rate, 1, C.byref(p))
        samples = [0.05*math.sin(2*math.pi*1000*i/rate) for i in range(rate//5)]
        out = process(lib, d, samples)
        gain = math.sqrt(sum(x*x for x in out[rate//10:])/sum(x*x for x in samples[rate//10:]))
        assert abs(20*math.log10(gain)) < .01, (rate, gain)
    # Every band, both shelf endpoints, gain/Q limits and low-rate fallback.
    for index in range(10):
        p = Preset(); lib.peq_default(C.byref(p)); p.bypass = 0
        p.bands[index].enabled = 1; p.bands[index].gain = -6
        e = Engine(); assert lib.peq_compile(C.byref(p), 48000, C.byref(e))
        assert abs(20*math.log10(response(e, 48000, p.bands[index].frequency)) + 6) < 1e-7
    for kind in (1, 2):
        for q in (0.1, 1/math.sqrt(2), 10):
            for gain in (-24, 24):
                p.bands[9] = Band(1, kind, 1000, gain, q)
                e = Engine(); assert lib.peq_compile(C.byref(p), 48000, C.byref(e))
                assert abs(20*math.log10(response(e, 48000, 0 if kind == 1 else 24000))-gain) < 1e-7
                for c in e.c:
                    assert abs(c.a2) < 1 and 1+c.a1+c.a2 > 0 and 1-c.a1+c.a2 > 0
    p.bands[9] = Band(1, 0, 16000, 6, 1)
    e = Engine(); assert lib.peq_compile(C.byref(p), 8000, C.byref(e)) and not e.bypass
    assert (e.c[9].b0, e.c[9].a1) == (1, 0)  # only the unrepresentable band is skipped
    p = parse(lib, '\n'.join(f'Filter: ON PK Fc {100+i*1000} Hz Gain 6 dB Q 1' for i in range(10)))
    d = DSP(); lib.peq_reset(C.byref(d), 48000, 2, C.byref(p))
    clipped = process(lib, d, [1., -1.] * 20000, 2)
    assert all(math.isfinite(x) and -1 <= x <= 1 for x in clipped)
    # Block splitting and stereo independence; no file I/O or allocation in process.
    a, b = DSP(), DSP()
    for d in (a, b): lib.peq_reset(C.byref(d), 48000, 2, C.byref(p))
    signal = [x for i in range(10000) for x in (0.02*math.sin(i*.13), 0.)]
    entire = process(lib, a, signal, 2)
    split = process(lib, b, signal[:2346], 2) + process(lib, b, signal[2346:], 2)
    assert entire == split and all(x == 0 for x in entire[1::2])
    p.bypass = 1
    assert lib.peq_update(C.byref(a), C.byref(p))
    process(lib, a, [0., 0.] * 960, 2)
    assert process(lib, a, signal, 2) == list((C.c_float * len(signal))(*signal))
    # Rapid updates queue the latest target without restarting the current fade.
    p.bypass = 0
    assert lib.peq_update(C.byref(a), C.byref(p))
    process(lib, a, [0., 0.] * 100, 2)
    old_ramp = a.ramp
    p.preamp = -24
    assert lib.peq_update(C.byref(a), C.byref(p)) and a.ramp == old_ramp
    process(lib, a, [0., 0.] * 2000, 2)
    assert not a.ramp and not a.waiting and abs(a.current.gain[0]-10**(-24/20)) < 1e-12
    lib.peq_reset(C.byref(a), 96000, 1, C.byref(p))
    assert process(lib, a, [0.] * 1000) == [0.] * 1000
    # Decaying tails settle at a ~-590 dB normal-float residue, never in the denormal range.
    tail = process(lib, a, [0.5] + [0.] * 96000)
    state = [abs(a.current.z[0][i][j]) for i in range(30) for j in range(2)]
    assert all(z < 1e-28 and (z == 0 or z > 1e-300) for z in state) and abs(tail[-1]) < 1e-28
    # A preamp-only change carries filter memory: the crossfade is a pure gain ramp.
    lib.peq_reset(C.byref(a), 48000, 1, C.byref(p))
    tone = [0.1*math.sin(2*math.pi*100*i/48000) for i in range(20000)]
    process(lib, a, tone[:10000])
    p.preamp = -30; assert lib.peq_update(C.byref(a), C.byref(p))
    after = process(lib, a, tone[10000:])
    b = DSP(); p.preamp = -24; lib.peq_reset(C.byref(b), 48000, 1, C.byref(p))
    reference = process(lib, b, tone)[10000:]
    fade = [1 - (1 - 10**(-6/20)) * min(i + 1, 960) / 960 for i in range(10000)]
    assert max(abs(x - r*f) for x, r, f in zip(after, reference, fade)) < 1e-6
    # A band switched off mid-play drains its carried memory before it is skipped, then passes x through.
    p = parse(lib, 'Filter: ON PK Fc 100 Hz Gain 6 dB Q 1')
    lib.peq_reset(C.byref(a), 48000, 1, C.byref(p))
    process(lib, a, tone[:10000])
    p.bands[0].gain = 0; assert lib.peq_update(C.byref(a), C.byref(p))
    process(lib, a, tone[:2000])
    assert all(a.current.z[0][i][j] == 0 for i in range(30) for j in range(2)) and a.current.used == 1
    assert process(lib, a, tone) == list((C.c_float * len(tone))(*tone))
    # One-channel bands and balance: left gets the band and 2 dB less, right neither.
    p = parse(lib, 'Channel: L\nFilter: ON PK Fc 1000 Hz Gain 6 dB Q 1'); p.balance = 2
    e = Engine(); assert lib.peq_compile(C.byref(p), 48000, C.byref(e))
    assert abs(20*math.log10(response(e, 48000, 1000, 0)) - 4) < 1e-7
    assert abs(20*math.log10(response(e, 48000, 1000, 1))) < 1e-12
    lib.peq_reset(C.byref(a), 48000, 2, C.byref(p))
    out = process(lib, a, [x for v in tone for x in (v, v)], 2)
    assert out[1::2] == list((C.c_float * len(tone))(*tone))
    gain = math.sqrt(sum(x*x for x in out[10000::2])/sum(x*x for x in tone[5000:]))
    assert abs(20*math.log10(gain) + 2) < .1, gain  # 100 Hz: the band barely reaches it
    # A both-channel band narrowed to the left drops the right's carried memory.
    p.bands[0].enabled = 1; lib.peq_reset(C.byref(a), 48000, 2, C.byref(p)); process(lib, a, [.5, .5] * 100, 2)
    p.bands[0].enabled = 2; assert lib.peq_update(C.byref(a), C.byref(p)); process(lib, a, [0., 0.] * 2000, 2)
    assert a.current.z[1][0][0] == a.current.z[1][0][1] == 0 and a.current.z[0][0][0]
    print('PEQ DSP: C PCM response, 30 bands, shelves, rates, bypass, clipping, channels, balance and updates passed.')

# Host stand-ins for the stock services peq_platform.h maps on the device.
SHIM_H = r"""
#include <dirent.h>
#include <sys/stat.h>
typedef int (*handler)(void *, void *);
extern volatile unsigned char g_equalizer_flag;
""" + ''.join(f'{r} {n}({a});\n' for n in """
list_item_create label_create list_view_create scroll_view_create widget_use_style
widget_set_text_utf8 widget_on widget_get_prop_int widget_set_prop_int widget_destroy_children
widget_resize scroll_view_set_offset widget_invalidate_force timer_add timer_remove
navigator_back write_int_config widget_factory widget_factory_create_widget widget_set_prop_str
widget_set_name widget_set_visible
canvas_set_fill_color canvas_fill_rect canvas_get_vgcanvas vgcanvas_save vgcanvas_restore vgcanvas_translate
vgcanvas_begin_path vgcanvas_close_path vgcanvas_move_to vgcanvas_line_to vgcanvas_arc vgcanvas_fill vgcanvas_stroke
vgcanvas_set_fill_color vgcanvas_set_stroke_color vgcanvas_set_fill_linear_gradient vgcanvas_set_line_width
""".split() for r, a in [PROTOTYPES[n]]) + r"""
"""
SHIM = r"""
static struct { int parent, h, bg; char text[160]; handler click, destroy, keyup; void *ctx; } w[4096];
static int count = 1, timers, removed, backs;
static int (*timer_fn)(const void *);
volatile unsigned char g_equalizer_flag;
int stock_eq_trampoline(int mode) { return mode; }
static void *make(void *parent, int h) {
    ++count; w[count].parent = (int)(long)parent; w[count].h = h;
    w[count].text[0] = 0; w[count].click = 0; w[count].bg = 0; return (void *)(long)count;
}
void *widget_factory(void) { return 0; }
void *widget_factory_create_widget(void *f, const char *type, void *p, int x, int y, int ww, int h) {
    (void)f; (void)type; (void)x; (void)y; (void)ww; return make(p, h);
}
int widget_set_name(void *x, const char *name) { (void)x; (void)name; return 0; }
int widget_set_visible(void *x, int v, int r) { (void)x; (void)v; (void)r; return 0; }
/* The curve's drawing (peq_paint) is checked on the device; here it only has to link. */
int canvas_set_fill_color(void *c, unsigned v) { (void)c; (void)v; return 0; }
int canvas_fill_rect(void *c, int x, int y, int ww, int h) { (void)c; (void)x; (void)y; (void)ww; (void)h; return 0; }
void *canvas_get_vgcanvas(void *c) { (void)c; return 0; }
#define VG(name, ...) int name(void *vg, ##__VA_ARGS__) { (void)vg; return 0; }
VG(vgcanvas_save) VG(vgcanvas_restore) VG(vgcanvas_begin_path) VG(vgcanvas_close_path) VG(vgcanvas_fill) VG(vgcanvas_stroke)
int vgcanvas_translate(void *vg, float x, float y) { (void)vg; (void)x; (void)y; return 0; }
int vgcanvas_move_to(void *vg, float x, float y) { (void)vg; (void)x; (void)y; return 0; }
int vgcanvas_line_to(void *vg, float x, float y) { (void)vg; (void)x; (void)y; return 0; }
int vgcanvas_arc(void *vg, float x, float y, float r, float a, float b, int ccw) { (void)vg; (void)x; (void)y; (void)r; (void)a; (void)b; (void)ccw; return 0; }
int vgcanvas_set_fill_color(void *vg, unsigned c) { (void)vg; (void)c; return 0; }
int vgcanvas_set_stroke_color(void *vg, unsigned c) { (void)vg; (void)c; return 0; }
int vgcanvas_set_line_width(void *vg, float w) { (void)vg; (void)w; return 0; }
int vgcanvas_set_fill_linear_gradient(void *vg, float a, float b, float c, float d, unsigned e, unsigned f) {
    (void)vg; (void)a; (void)b; (void)c; (void)d; (void)e; (void)f; return 0;
}
void draw_centred(void *canvas, const unsigned *s, unsigned n, const void *r, unsigned px, unsigned color) {
    (void)canvas; (void)s; (void)n; (void)r; (void)px; (void)color;
}
int widget_set_prop_str(void *x, const char *k, const char *v) { (void)x; (void)k; (void)v; return 0; }
void *list_item_create(void *p, int x, int y, int ww, int h) { (void)x; (void)y; (void)ww; return make(p, h); }
void *label_create(void *p, int x, int y, int ww, int h) { (void)x; (void)y; (void)ww; return make(p, h); }
void *list_view_create(void *p, int x, int y, int ww, int h) { (void)x; (void)y; (void)ww; return make(p, h); }
void *scroll_view_create(void *p, int x, int y, int ww, int h) { (void)x; (void)y; (void)ww; return make(p, h); }
int widget_use_style(void *x, const char *s) { (void)x; (void)s; return 0; }
int widget_set_text_utf8(void *x, const char *s) { snprintf(w[(long)x].text, 160, "%s", s); return 0; }
unsigned widget_on(void *x, unsigned type, handler f, void *ctx) {
    long i = (long)x;
    if (type == 0x10c) { w[i].click = f; w[i].ctx = ctx; }
    else if (type == 0x0c) w[i].destroy = f;
    else w[i].keyup = f;
    return 1;
}
int widget_get_prop_int(void *x, const char *k, int d) { return (long)x == 1 && !strcmp(k, "h") ? 290 : d; }
int widget_set_prop_int(void *x, const char *k, int v) {
    if (!strcmp(k, "style:normal:bg_color")) w[(long)x].bg = v;
    return 0;
}
int widget_destroy_children(void *x) { (void)x; count = 1; return 0; }
int widget_resize(void *x, int ww, int h) { (void)ww; w[(long)x].h = h; return 0; }
int scroll_view_set_offset(void *x, int a, int b) { (void)x; (void)a; (void)b; return 0; }
int widget_invalidate_force(void *x, void *y) { (void)x; (void)y; return 0; }
unsigned timer_add(int (*f)(const void *), void *ctx, unsigned ms) { (void)ctx; (void)ms; timer_fn = f; return ++timers; }
int timer_remove(unsigned id) { (void)id; timer_fn = 0; ++removed; return 0; }
int navigator_back(void) { return ++backs; }
static int eqflag = -1;
int write_int_config(int v, const char *s, const char *k) { if (!strcmp(s, "PLAYSET") && !strcmp(k, "EQFLAG")) eqflag = v; return 0; }
int shim_eqflag(void) { return eqflag; }

int peq_page_init(void *page, void *context);
int shim_open(void) { count = 1; w[1].destroy = w[1].keyup = 0; return peq_page_init((void *)1, 0); }
int shim_pending(void) { return timer_fn != 0; }
int shim_removed(void) { return removed; }
unsigned char shim_flag(void) { return g_equalizer_flag; }
void shim_run(void) { int (*f)(const void *) = timer_fn; timer_fn = 0; if (f) f(0); }
/* Click the row whose labels, caption and value joined by a space, start with text; optionally let
 * the deferred render run. */
int shim_click(const char *text, int run) {
    for (int p = 2; p <= count; ++p) {
        char joined[400] = "";
        for (int i = p + 1; i <= count; ++i)
            if (w[i].parent == p) snprintf(joined + strlen(joined), sizeof(joined) - strlen(joined), "%s%s", *joined ? " " : "", w[i].text);
        if (w[p].click && *joined && !strncmp(joined, text, strlen(text))) {
            w[p].click(w[p].ctx, 0);
            if (run) shim_run();
            return 1;
        }
    }
    return 0;
}
void shim_return(void) { int event[8] = {0}; event[6] = 170; w[1].keyup(0, event); shim_run(); }
void shim_close(void) { w[1].destroy(0, 0); }
/* The value screen's large value (the page's 56px label). */
const char *shim_value(void) {
    for (int i = 2; i <= count; ++i) if (w[i].parent == 1 && w[i].h == 56) return w[i].text;
    return "";
}
const char *shim_title(void) {
    for (int i = 2; i <= count; ++i) if (w[i].parent == 1 && w[i].h == 48) return w[i].text;
    return "";
}
int shim_list_height(void) { return w[2].h; } /* the list view is the page's first child */
unsigned shim_list_bg(void) { return (unsigned)w[2].bg; }
"""

def editor_check(lib, tmp):
    """The editor's promises, driven through its real click and render paths."""
    (tmp/'shim.h').write_text(SHIM_H)
    (tmp/'shim.c').write_text('#include "peq.h"\n' + SHIM)
    ui = compile_host(tmp, 'peq_ui.so', ROOT/'patch/peq_ui.c', ROOT/'patch/peq.c', tmp/'shim.c',
                      '-I', ROOT/'patch', '-include', tmp/'shim.h')
    ui.shim_title.restype = ui.shim_value.restype = C.c_char_p
    ui.peq_key.argtypes = [C.c_void_p, C.c_uint, C.c_int]
    data, saved = tmp/'root/mnt/data', tmp/'root/mnt/data/peq-presets'
    saved.mkdir(parents=True); (tmp/'root/mnt/mmc/EQ').mkdir(parents=True)
    active = data/'peq-active'
    def preset(**band):
        p = Preset(); lib.peq_default(C.byref(p)); p.bypass = 0
        for k, v in band.items(): setattr(p.bands[0], k, v)
        return p
    def read():
        p = Preset(); assert lib.peq_load(bytes(active), C.byref(p)); return p
    def click(text): assert ui.shim_click(text.encode(), 1), (text, ui.shim_title())
    title = lambda: ui.shim_title().decode()
    value = lambda: ui.shim_value().decode()
    # The value screen: the wheel (n steps, as ringnav's spin ramp scales them) and Centre, which finishes.
    def wheel(n): assert ui.peq_key(1, 173 if n > 0 else 172, abs(n)) == 1
    def centre(): assert ui.peq_key(1, 218, 0) == 1; ui.shim_run()

    assert lib.peq_save(bytes(active), C.byref(preset(enabled=1)), 1) == 1
    # With the stock flag clear no filter runs, so the saved ON reads OFF.
    assert ui.shim_open() == 0 and title() == 'PEQ'
    # The visualizer's attach puts the filter in the chain with PEQ off: the saved ON becomes OFF, so it stays silent.
    ui.shim_close(); ui.peq_attach(); assert read().bypass == 1 and ui.shim_flag() == 0; ui.shim_open()
    assert lib.peq_save(bytes(active), C.byref(preset(enabled=1)), 1) == 1
    # The switch writes the stock config key, so boot restores it; the flag follows the preset.
    click('PEQ Off'); assert ui.shim_eqflag() == 1 and ui.shim_flag() == 1
    click('PEQ On'); assert ui.shim_eqflag() == 0 and ui.shim_flag() == 0
    ui.shim_close(); assert ui.shim_open() == 0; click('PEQ Off')
    full = ui.shim_list_height()
    # iPod rows are transparent, so the list itself must paint black, not the theme's light card.
    ui.shim_list_bg.restype = C.c_uint; assert ui.shim_list_bg() == 0xff000000
    # Bypass switches at once but keeps unapplied band edits out of the active preset.
    click('1  '); click('Gain +0.0 dB'); assert title() == 'PEQ Band 1 Gain' and value() == '+0.0 dB'
    wheel(2); assert value() == '+1.0 dB'; centre(); ui.shim_return()
    click('PEQ On')
    assert read().bypass == 1 and read().bands[0].gain == 0 and ui.shim_flag() == 0
    click('Apply changes')
    assert read().bands[0].gain == 1 and read().bypass == 1 and title() == 'Applied'
    # A band edit sets the preamp to minus the combined response's peak: here the one +1 dB band.
    assert abs(read().preamp + 1) < 0.01, read().preamp
    # A message takes the title bar; the list keeps every row.
    assert ui.shim_list_height() == full == 144  # 3 rows under the curve
    # Loading into the editor does not activate it.
    assert lib.peq_save(bytes(saved/'HD650.peq'), C.byref(preset(enabled=1, gain=-3.0)), 1) == 1
    before = active.read_bytes()
    click('Presets'); click('HD650.peq')
    assert active.read_bytes() == before and title() == 'Preset loaded; choose Apply to activate'
    click('PEQ Off'); click('PEQ On')  # any action clears the message
    assert title() == 'PEQ: HD650' and read().bands[0].gain == 1  # the draft's name; still the applied preset
    # A failed apply or switch leaves the active preset untouched.
    before = active.read_bytes()
    (data/'peq-active.tmp').mkdir()
    click('Apply changes')
    assert title() == 'Apply failed; active EQ unchanged' and active.read_bytes() == before
    click('PEQ Off')
    assert title() == 'Switch failed; PEQ unchanged' and active.read_bytes() == before
    (data/'peq-active.tmp').rmdir()
    click('Apply changes'); assert read().bands[0].gain == -3
    # Closing cancels the pending render.
    assert ui.shim_click(b'Presets', 0) and ui.shim_pending()
    removed = ui.shim_removed()
    ui.shim_close()
    assert not ui.shim_pending() and ui.shim_removed() == removed + 1
    ui.shim_open()
    assert title() == 'PEQ: HD650'  # the applied preset's name survives the page
    # Deleting asks first, removes only the saved copy and leaves the active EQ alone.
    before = active.read_bytes()
    click('Presets'); click('Delete a preset'); click('HD650.peq'); click('Cancel')
    assert (saved/'HD650.peq').exists()
    click('HD650.peq'); click('Delete HD650.peq? Confirm')
    assert not (saved/'HD650.peq').exists() and active.read_bytes() == before
    assert title() == 'Deleted HD650.peq; active EQ unchanged' and not ui.shim_click(b'HD650.peq', 0)
    # Overlapping boosts add up (+6.5 and +6 dB at 1 kHz); cuts alone leave 0 dB.
    p = preset(enabled=1, gain=6.0); p.count = 2; p.bands[1] = p.bands[0]; p.preamp = -1
    assert lib.peq_save(bytes(active), C.byref(p), 1) == 1
    ui.shim_close(); ui.shim_open(); click('1  '); click('Gain +6.0 dB'); wheel(1); centre(); ui.shim_return(); click('Apply changes')
    assert read().preamp == -1, read().preamp  # a preamp off Auto (here the preset's) sticks through band edits
    click('Preamp -1.0 dB'); assert title() == 'PEQ Preamp' and value() == '-1.0 dB'
    wheel(30); assert value() == 'Auto -12.5 dB'; centre(); click('Apply changes')  # Auto is past the top
    assert abs(read().preamp + 12.5) < 0.05, read().preamp
    click('1  '); click('Band On'); ui.shim_return(); click('2  '); click('Band On'); ui.shim_return(); click('Apply changes')
    assert math.copysign(1, read().preamp) == 1 and read().preamp == 0, read().preamp  # +0: shown as 0.0
    # Channels cycle on enabled bands; headroom takes the louder side, balance only turns one down.
    click('1  Peaking 31 Hz Off'); click('Gain +6.5 dB'); wheel(2); centre(); click('Channels Both')  # scrubbing a gain turns the band on
    assert title() == 'PEQ Band 1'
    click('Channels Left'); ui.shim_return()
    # Balance scrubs L 12 to R 12 dB in 0.5 dB steps from the current value, live, clamped at the ends.
    click('Balance Centre'); assert title() == 'PEQ Balance' and value() == 'Centre'
    wheel(30); assert value() == 'R 12.0 dB'; centre(); assert title() == 'PEQ'
    click('Balance R 12.0 dB'); wheel(-25); assert value() == 'L 0.5 dB'
    ui.shim_return(); assert title() == 'PEQ'; click('Apply changes')
    r = read(); assert (r.bands[0].enabled, r.balance) == (3, -0.5) and abs(r.preamp + 7.5) < 0.05, (r.bands[0].enabled, r.balance)
    assert ui.shim_click(b'1  Peaking 31 Hz R +7.5 dB', 0) and ui.shim_click(b'Balance L 0.5 dB', 0)
    # Frequency scrubs in 1/48 octave and Q in 1/24, at least 1 Hz or 0.01 a step, within 20 Hz to 20 kHz
    # and 0.1 to 10.
    ui.shim_close(); ui.shim_open(); click('1  Peaking 31 Hz R'); click('Frequency 31 Hz')
    assert title() == 'PEQ Band 1 Frequency (Hz)' and value() == '31 Hz'
    wheel(-1); assert value() == '30 Hz'  # 30.6 rounds back to 31, so the step is 1 Hz
    wheel(-100); assert value() == '20 Hz'
    wheel(48); assert value() == '40 Hz'  # an octave
    wheel(1000); assert value() == '20000 Hz'
    wheel(-4 * 48); assert value() == '1250 Hz'
    ui.shim_return(); assert title() == 'PEQ Band 1'
    click('Q 1.41'); assert title() == 'PEQ Band 1 Q' and value() == '1.41'
    wheel(24); assert value() == '2.82'
    wheel(-1000); assert value() == '0.10'
    wheel(1); assert value() == '0.11'
    centre(); ui.shim_return(); click('Apply changes')
    r = read(); assert (r.bands[0].frequency, round(r.bands[0].q, 9)) == (1250, 0.11), (r.bands[0].frequency, r.bands[0].q)
    print('PEQ editor: bypass, apply, auto preamp, channels, balance, frequency and Q, load, failed saves, delete and close passed.')

# Drives patch/peq_player.c the way hciplayer's af chain does. Built 32-bit like the device,
# so the file's ABI asserts hold; checked against the shared DSP driven directly.
PLAYER = r"""
#include <assert.h>
#include "peq.h"
int mp3_toc(const unsigned char *h, unsigned n, double t, double *frac, double *length);
int peq_open(af_instance *af);

char *peq_mpctx; /* the player's MPContext pointer; null: no codec to check */
static int left = -1; /* calloc calls before one fails; -1: never */
void *test_calloc(size_t n, size_t size) {
    if (!left--) return 0;
    void *p = malloc(n * size);
    return p ? memset(p, 0, n * size) : p;
}

static peq_preset active(double preamp, double gain) {
    peq_preset p;
    peq_default(&p);
    p.bypass = 0; p.preamp = preamp; p.count = 1;
    p.bands[0] = (peq_band){1, 0, 1000, gain, 1};
    assert(peq_save(PEQ_ACTIVE, &p, 1) == 1);
    return p;
}

static int negotiate(af_instance *af, af_data *in) { return af->control(af, 0x10000100, in); }

/* The visualizer's tap (demo creates the file before the player maps it). */
static vis_tap *tap_view(void) {
    static vis_tap *t;
    if (!t) {
        int fd = open(VIS_FILE, O_RDWR | O_CREAT, 0644);
        assert(fd >= 0 && !ftruncate(fd, sizeof(vis_tap)));
        t = mmap(0, sizeof(vis_tap), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        assert(t != MAP_FAILED && !close(fd));
    }
    return t;
}

/* One block through the filter equals the same block through the reference DSP, and is filtered. */
static void same(af_instance *af, peq_dsp *ref, int rate, int nch) {
    float a[1024], b[1024], in[1024];
    unsigned frames = 1024 / nch;
    for (int i = 0; i < 1024; ++i) a[i] = b[i] = in[i] = 0.5f * sinf(i * 0.05f);
    af_data d = {a, (int)(frames * nch * 4), rate, nch, 0x1d, 4};
    ++tap_view()->want; /* the visualizer is watching */
    assert(af->play(af, &d) == &d);
    peq_process(ref, b, frames);
    assert(!memcmp(a, b, sizeof(a)) && memcmp(a, in, sizeof(a)));
    /* The tap ends with what played: the first two channels, every rate / 44100th frame above 48 kHz. */
    vis_tap *t = tap_view();
    unsigned step = rate > 48000 ? (unsigned)rate / 44100 : 1, n = (frames + step - 1) / step;
    assert(t->rate == (unsigned)rate / step && t->stamp > 0);
    for (unsigned i = 0; i < n; ++i)
        for (int ch = 0; ch < 2; ++ch)
            assert(t->ring[(t->seq - n + i) % VIS_RING][ch] == a[i * step * nch + (ch && nch > 1)]);
}

int main(void) {
    af_instance af;
    tap_view();
    /* A failed open is cleaned up by af_create calling uninit on the partial state. */
    for (int n = 0; n < 2; ++n) {
        memset(&af, 0, sizeof(af));
        left = n;
        assert(peq_open(&af) == -2);
        af.uninit(&af);
        assert(!af.data && !af.setup);
    }
    left = -1;
    memset(&af, 0, sizeof(af));
    assert(peq_open(&af) == 1 && af.mul == 1 && !af.delay && af.data && af.setup);
    player_state *s = af.setup;

    /* Format negotiation: bad channel counts fail, unsupported rates and formats decline,
       other formats are asked to convert to float, and float is accepted. */
    af_data in = {0, 0, 48000, 0, 0x1d, 4};
    assert(negotiate(&af, 0) == -2);
    assert(negotiate(&af, &in) == -2);
    in.nch = PEQ_CHANNELS + 1; assert(negotiate(&af, &in) == -2);
    in.nch = 2; in.rate = 7999; assert(negotiate(&af, &in) == 2);
    in.rate = 384001; assert(negotiate(&af, &in) == 2);
    in.rate = 48000; in.format = 64; assert(negotiate(&af, &in) == 2);
    /* DSD detaches, by the vendor's dsdiff driver or an FFmpeg ffdsd codec; other codecs negotiate. */
    static char *mp[12], *sh[2], *codec[0x3e4 / 4];
    peq_mpctx = (char *)mp; mp[0x2c / 4] = (char *)sh; sh[1] = (char *)codec;
    in.format = 0x1d;
    codec[0x3e0 / 4] = "dsdiff"; assert(negotiate(&af, &in) == 2);
    codec[0x3e0 / 4] = "ffmpeg"; codec[0x3d0 / 4] = "ffdsdmsbf"; assert(negotiate(&af, &in) == 2);
    codec[0x3d0 / 4] = "ffflac"; assert(negotiate(&af, &in) == 1);
    peq_mpctx = 0;
    peq_preset first = active(-6, 6);
    in.format = 0x11; in.bps = 2;
    assert(negotiate(&af, &in) == 0 && in.format == 0x1d && in.bps == 4);
    assert(in.rate == 48000 && in.nch == 2);
    assert(negotiate(&af, &in) == 1);
    assert(af.data->format == 0x1d && af.data->bps == 4);
    assert(af.data->rate == 48000 && af.data->nch == 2);
    assert(s->dsp.rate == 48000 && s->dsp.channels == 2);
    assert(!memcmp(&s->preset, &first, sizeof(first)));
    peq_dsp ref;
    peq_reset(&ref, 48000, 2, &first);
    same(&af, &ref, 48000, 2);

    /* Buffers not in the negotiated format pass through untouched and leave the state alone. */
    float a[64] = {0.25f}, keep[64];
    memcpy(keep, a, sizeof(a));
    af_data bad[] = {
        {a, 256, 44100, 2, 0x1d, 4}, {a, 256, 48000, 1, 0x1d, 4}, {a, 256, 48000, 2, 0x11, 4},
        {a, 256, 48000, 2, 0x1d, 2}, {a, 252, 48000, 2, 0x1d, 4}, {a, 0, 48000, 2, 0x1d, 4},
    };
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i)
        assert(af.play(&af, &bad[i]) == &bad[i] && !memcmp(a, keep, sizeof(a)));
    assert(af.play(&af, 0) == 0);
    same(&af, &ref, 48000, 2);

    /* Live updates arrive through the stock gain query on the playback loop, channel 0 only. */
    float gains[10]; /* stock's graphic EQ array */
    struct { float *gain; int channel; } ext = {gains, 1};
    peq_preset second = active(-3, -4);
    assert(af.control(&af, 0x40001d00, &ext) == 1 && !memcmp(&s->preset, &first, sizeof(first)));
    same(&af, &ref, 48000, 2);
    ext.channel = 0;
    assert(af.control(&af, 0x40001d00, &ext) == 1 && !memcmp(&s->preset, &second, sizeof(second)));
    assert(peq_update(&ref, &second));
    for (int i = 0; i < 4; ++i) same(&af, &ref, 48000, 2); /* through the crossfade */
    /* An unchanged or unreadable active file keeps the current filter. */
    assert(af.control(&af, 0x40001d00, &ext) == 1);
    FILE *f = fopen(PEQ_ACTIVE, "wb");
    assert(f && fputs("junk", f) >= 0 && !fclose(f));
    assert(af.control(&af, 0x40001d00, &ext) == 1 && !memcmp(&s->preset, &second, sizeof(second)));
    same(&af, &ref, 48000, 2);
    /* The stock graphic gains read back as flat; malformed queries fail. */
    for (int i = 0; i < 10; ++i) gains[i] = 1;
    assert(af.control(&af, 0x40001d01, &ext) == 1);
    for (int i = 0; i < 10; ++i) assert(gains[i] == 0);
    assert(af.control(&af, 0x40001d00, 0) == -2);
    ext.channel = PEQ_CHANNELS; assert(af.control(&af, 0x40001d00, &ext) == -2);
    ext.channel = -1; assert(af.control(&af, 0x40001d00, &ext) == -2);
    ext.channel = 0; ext.gain = 0; assert(af.control(&af, 0x40001d00, &ext) == -2);
    assert(af.control(&af, 0x20000300, 0) == 1 && af.control(&af, 0x12345, 0) == -1);

    /* A new track renegotiates: new rate and channels, the active preset reread, fresh state. */
    peq_preset third = active(0, 3);
    in = (af_data){0, 0, 96000, 1, 0x1d, 4};
    assert(negotiate(&af, &in) == 1 && s->dsp.rate == 96000 && s->dsp.channels == 1);
    assert(!memcmp(&s->preset, &third, sizeof(third)));
    peq_reset(&ref, 96000, 1, &third);
    same(&af, &ref, 96000, 1);
    memcpy(a, keep, sizeof(a));
    af_data old = {a, 256, 48000, 2, 0x1d, 4};
    assert(af.play(&af, &old) == &old && !memcmp(a, keep, sizeof(a)));
    /* Without an active file a track starts on the defaults. */
    assert(!unlink(PEQ_ACTIVE));
    in.rate = 44100;
    assert(negotiate(&af, &in) == 1);
    peq_preset defaults;
    peq_default(&defaults);
    assert(!memcmp(&s->preset, &defaults, sizeof(defaults)));

    /* With nobody watching for a second of audio, the tap stops copying; a bump resumes it. */
    vis_tap *t = tap_view();
    unsigned seq = t->seq;
    float block[2 * 1000] = {0};
    af_data idle = {block, (int)sizeof(block), 44100, 1, 0x1d, 4};
    for (int i = 0; i < 100; ++i) af.play(&af, &idle); /* 200000 frames, past 44100 */
    unsigned after = t->seq;
    assert(after != seq && after - seq < 50000);
    af.play(&af, &idle); assert(t->seq == after);
    ++t->want; af.play(&af, &idle); assert(t->seq == after + 2000);

    af.uninit(&af);
    assert(!af.data && !af.setup);

    /* Exact VBR seeking: a 44.1 kHz stereo MPEG-1 Layer III Xing frame, 3600 s long, whose table
     * puts the first half of the time in the first quarter of the bytes. */
    unsigned char h[192] = {0xff, 0xfb, 0x90, 0x00};
    unsigned frames = 3600 * 44100 / 1152; /* 137812 frames, 3599.98 s */
    memcpy(h + 36, "Xing\0\0\0\x07", 8);
    h[44] = frames >> 24; h[45] = frames >> 16; h[46] = frames >> 8; h[47] = frames;
    for (int i = 0; i < 100; i++) h[52 + i] = i < 50 ? i * 64 / 50 : 64 + (i - 50) * 192 / 50;
    double frac, length;
    assert(mp3_toc(h, sizeof h, 0, &frac, &length) && frac == 0 && fabs(length - frames * 1152.0 / 44100) < 1e-9);
    assert(mp3_toc(h, sizeof h, length / 2, &frac, &length) && fabs(frac - 0.25) < 1e-9);
    assert(mp3_toc(h, sizeof h, length / 4, &frac, &length) && fabs(frac - 0.125) < 1e-9);
    assert(mp3_toc(h, sizeof h, length * 0.995, &frac, &length) && frac > 0.98 && frac < 1);
    assert(mp3_toc(h, sizeof h, length, &frac, &length) && frac == 1);
    assert(!mp3_toc(h, 100, 10, &frac, &length));        /* cut short */
    h[43] = 3;                                            /* no table */
    assert(!mp3_toc(h, sizeof h, 10, &frac, &length));
    h[43] = 7; memcpy(h + 36, "Info", 4);                 /* CBR: stock is already exact */
    assert(!mp3_toc(h, sizeof h, 10, &frac, &length));
    memcpy(h + 36, "Xing", 4); h[1] = 0xfd;               /* Layer III only */
    assert(!mp3_toc(h, sizeof h, 10, &frac, &length));
    h[1] = 0xf3; h[2] = 0x90;                             /* MPEG-2, 22.05 kHz: side info 17, 576 a frame */
    memmove(h + 21, h + 36, 120);
    assert(mp3_toc(h, sizeof h, 0, &frac, &length) && fabs(length - frames * 576.0 / 22050) < 1e-9);
    return 0;
}
"""

def player_check(tmp):
    """hciplayer's filter: negotiation, track changes, live updates and cleanup."""
    root = tmp/'player'
    (root/'mnt/data').mkdir(parents=True)
    (root/'tmp').mkdir()
    (tmp/'player_test.c').write_text(PLAYER)
    binary = tmp/'player_test'
    sources = [ROOT/'patch/peq_player.c', ROOT/'patch/peq.c', tmp/'player_test.c']
    subprocess.run(['cc', '-m32', '-DPEQ_HOST', f'-DPEQ_ROOT="{root}"', '-Dcalloc=test_calloc',
                    '-O2', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT/'patch'),
                    *map(str, sources), '-lm', '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
    print('PEQ player: negotiation, pass-through, the visualizer tap, live updates, track changes, cleanup and the Xing seek table passed.')

def visualizer_check(tmp):
    """The visualizer's analysis (patch/visualizer.c vis_bands): a sine lands in its band at full scale."""
    lib = compile_host(tmp, 'visualizer.so', ROOT/'patch/visualizer.c', '-DIPOD=1', '-I', ROOT/'patch')
    rate, n, bands = 44100, 1024, 64
    def levels(pcm):
        out = (C.c_float * bands)()
        lib.vis_bands((C.c_float * (2 * n))(*pcm), rate, out)
        return list(out)
    assert levels([0.0] * (2 * n)) == [0.0] * bands  # silence rests
    edge = lambda b: int(40 * 400 ** (b / bands) * n / rate + 0.5)  # the C band_bin
    for k in (12, 100, 300):  # a sine exactly on bin k, in both channels
        pcm = [math.sin(2 * math.pi * k * i / n) for i in range(n) for _ in (0, 1)]
        got, want = levels(pcm), max(b for b in range(bands) if edge(b) <= k)
        assert got[want] > 0.99 and got.index(max(got)) <= want, (k, want, got)
        assert all(v == 0 for b, v in enumerate(got) if edge(b) > k + 1 or edge(b + 1) < k - 1), (k, got)
        assert abs(levels([v / 2 for v in pcm])[want] - (1 - 6.0206 / 60)) < 0.01  # half scale: 6 dB down
    print('Visualizer: FFT bands of silence and sines, and their levels, passed.')

SCROBBLE = r"""
#include <assert.h>
#include <stdarg.h>
#include "peq.h"
int scrobble_ready(void), scrobble_start(void), scrobble_poll(int *);
void scrobble_append(const char *, unsigned);

static const char *cfg[5]; /* TOKEN, USER, PASSWORD, API_KEY, API_SECRET */
int toolsReadConfig(const char *path, const char *section, const char *key, char *out, const char *def) {
    static const char *const keys[] = { "TOKEN", "USER", "PASSWORD", "API_KEY", "API_SECRET" };
    (void)def;
    assert(strstr(path, "/mnt/mmc/.scrobble.ini"));
    for (int i = 0; i < 5; i++)
        if (!strcmp(key, keys[i]) && !strcmp(section, i ? "LASTFM" : "LISTENBRAINZ") && cfg[i]) { strcpy(out, cfg[i]); return 1; }
    return -1; /* stock leaves out alone for a missing file or key */
}

/* libcurl: every request is dumped as url, headers, verify, body; replies are scripted. */
typedef unsigned (*writer)(const char *, unsigned, unsigned, void *);
typedef struct node { const char *s; struct node *next; } node;
static struct { const char *url, *body; node *h; writer w; void *ctx; long verify, code; } easy;
static int requests, fail_at = -1, no_key, append_at = -1;
static FILE *dump;
void *curl_easy_init(void) { memset(&easy, 0, sizeof easy); return &easy; }
void *curl_slist_append(void *list, const char *s) {
    node *n = calloc(1, sizeof *n), *l = list;
    n->s = s;
    if (!l) return n;
    while (l->next) l = l->next;
    l->next = n;
    return list;
}
void curl_slist_free_all(void *list) { for (node *n = list, *x; n; n = x) x = n->next, free(n); }
int curl_easy_setopt(void *c, int opt, ...) {
    va_list a;
    va_start(a, opt);
    assert(c == &easy);
    if (opt == 10002) easy.url = va_arg(a, const char *);
    else if (opt == 10015) easy.body = va_arg(a, const char *);
    else if (opt == 10023) easy.h = va_arg(a, node *);
    else if (opt == 20011) easy.w = va_arg(a, writer);
    else if (opt == 10001) easy.ctx = va_arg(a, void *);
    else if (opt == 64) easy.verify = va_arg(a, long);
    else if (opt == 10065) assert(strstr(va_arg(a, const char *), ".scrobble.pem"));
    else assert(opt == 99 || opt == 13 || opt == 78 || opt == 81);
    va_end(a);
    return 0;
}
int curl_easy_perform(void *c) {
    (void)c;
    int i = requests++;
    if (i == append_at) scrobble_append("New\tB\tListen\t\t100\tL\t1700009999\t\n", 33); /* a listen meanwhile */
    fprintf(dump, "%s\t", easy.url);
    for (node *n = easy.h; n; n = n->next) fprintf(dump, "%s|", n->s);
    fprintf(dump, "\t%ld\t%s\n", easy.verify, easy.body);
    const char *reply = !strstr(easy.url, "audioscrobbler") ? "{\"status\":\"ok\"}"
                        : strstr(easy.body, "auth.getMobileSession") ? (no_key ? "{\"session\":{}}" : "{\"session\":{\"name\":\"u\",\"key\":\"SK123\"}}")
                        : "{\"scrobbles\":{\"@attr\":{\"accepted\":1,\"ignored\":0}}}";
    easy.code = i == fail_at ? 500 : 200;
    easy.w(reply, 1, (unsigned)strlen(reply), easy.ctx);
    return 0;
}
int curl_easy_getinfo(void *c, int opt, ...) {
    va_list a;
    va_start(a, opt);
    assert(c == &easy && opt == 0x200002);
    *va_arg(a, long *) = easy.code;
    va_end(a);
    return 0;
}
void curl_easy_cleanup(void *c) { assert(c == &easy); }

static char *slurp(const char *path) {
    static char s[1 << 20];
    FILE *f = fopen(path, "rb");
    size_t n = f ? fread(s, 1, sizeof s - 1, f) : 0;
    if (f) fclose(f);
    s[n] = 0;
    return f ? s : 0;
}
static int lines(const char *s) { int n = 0; for (; s && *s; s++) n += *s == '\n'; return n; }
static int run(int *sent) {
    int r, started = scrobble_start();
    if (started <= 0) return started - 10;
    while (!(r = scrobble_poll(sent))) usleep(1000);
    assert(!scrobble_poll(sent)); /* reported once */
    return r;
}
static void write_log(int n) {
    FILE *f = fopen(ROOT "/mnt/mmc/.scrobbler.log", "wb");
    fputs("#AUDIOSCROBBLER/1.1\n#TZ/UTC\n#CLIENT/Q2 Pod\n", f);
    for (int i = 0; i < n; i++)
        fprintf(f, "Art \"%d\" \\ &=+%%\xc3\xa9\t%s\tTitle %d\t%d\t%d\tL\t%d\t\n", i, i % 10 ? "Alb/um" : "", i, i, 200 + i, 1700000000 + i);
    fputs("Skipped\tA\tT\t\t200\tS\t1700000000\t\njunk\n# a comment\n", f);
    fclose(f);
}
#define LOG ROOT "/mnt/mmc/.scrobbler.log"
#define HEADER "#AUDIOSCROBBLER/1.1\n#TZ/UTC\n#CLIENT/Q2 Pod\n"

int main(void) {
    int sent = -1;
    dump = fopen(ROOT "/requests", "w");
    setvbuf(dump, 0, _IONBF, 0);
    assert(!scrobble_ready() && run(&sent) == -11); /* no accounts: no row, and never an upload */
    cfg[0] = "tok";
    assert(scrobble_ready() == 1);
    cfg[1] = "u", cfg[2] = "p&w", cfg[3] = "key";
    assert(scrobble_ready() == 1); /* Last.fm needs all four */
    cfg[4] = "sec";
    assert(scrobble_ready() == 3);
    assert(run(&sent) == 1 && !sent && !requests); /* no log: nothing to upload */

    write_log(120);
    append_at = 0;
    assert(run(&sent) == 1 && sent == 120 && requests == 7); /* 50, 50, 20 to each, one session */
    assert(!strcmp(slurp(LOG), HEADER "New\tB\tListen\t\t100\tL\t1700009999\t\n"));
    char *s = slurp(LOG ".sent");
    assert(lines(s) == 122 && !strstr(s, "#") && strstr(s, "junk\n"));

    write_log(60); /* ListenBrainz fails the second batch: the first leaves, the rest stays */
    append_at = -1, fail_at = 10;
    assert(run(&sent) == -1 && sent == 50);
    s = slurp(LOG);
    assert(!strncmp(s, HEADER "Art \"50\"", sizeof HEADER + 7) && lines(s) == 3 + 10 + 3);
    assert(lines(slurp(LOG ".sent")) == 172);

    char before[1 << 16];
    strcpy(before, slurp(LOG)); /* no session key: nothing changes */
    fail_at = -1, no_key = 1;
    assert(run(&sent) == -1 && !sent && !strcmp(slurp(LOG), before) && lines(slurp(LOG ".sent")) == 172);
    cfg[1] = 0, no_key = 0; /* ListenBrainz only, with a CA bundle on the card: verified */
    fclose(fopen(ROOT "/mnt/mmc/.scrobble.pem", "w"));
    assert(run(&sent) == 1 && sent == 10 && !strcmp(slurp(LOG), HEADER));
    fclose(dump);
    return 0;
}
"""

def scrobble_check(tmp):
    """Upload Scrobbles: config, log parsing and batching, the ListenBrainz JSON, the signed Last.fm
    form, and the log rewrite, on a real pthread with libcurl scripted and the host's libcrypto MD5."""
    import hashlib, json, urllib.parse
    root = tmp/'scrobble'
    (root/'mnt/mmc').mkdir(parents=True)
    shim = ['#include <pthread.h>', '#define pthread_mutex_lock(m) pthread_mutex_lock((pthread_mutex_t *)(m))',
            '#define pthread_mutex_unlock(m) pthread_mutex_unlock((pthread_mutex_t *)(m))', '#define tk_snprintf snprintf']
    shim += [f'{PROTOTYPES[n][0]} {n}({PROTOTYPES[n][1]});' for n in PROTOTYPES if n.startswith(('curl_', 'MD5_', 'toolsReadConfig'))]
    (tmp/'scrobble_shim.h').write_text('\n'.join(shim) + '\n')
    (tmp/'scrobble_test.c').write_text(SCROBBLE)
    binary = tmp/'scrobble_test'
    subprocess.run(['cc', '-pthread', '-DPEQ_HOST', f'-DPEQ_ROOT="{root}"', f'-DROOT="{root}"', '-D_GNU_SOURCE',
                    '-O1', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT/'patch'), '-include', str(tmp/'scrobble_shim.h'),
                    str(ROOT/'patch/scrobble.c'), str(tmp/'scrobble_test.c'), '-lcrypto', '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
    requests = [line.split('\t', 3) for line in (root/'requests').read_text().splitlines()]
    artist = lambda i: f'Art "{i}" \\ &=+%é'
    lb_sizes, fm_sizes = [], []
    for url, headers, verify, body in requests:
        if 'listenbrainz' in url:
            assert url == 'https://api.listenbrainz.org/1/submit-listens'
            assert headers == 'Authorization: Token tok|Content-Type: application/json|'
            listens = json.loads(body)['payload']
            assert json.loads(body)['listen_type'] == 'import'
            for x in listens:
                i = x['listened_at'] - 1700000000; m = x['track_metadata']
                assert m['artist_name'] == artist(i) and m['track_name'] == f'Title {i}'
                assert m.get('release_name') == ('Alb/um' if i % 10 else None)
            lb_sizes.append(len(listens))
            continue
        assert url == 'https://ws.audioscrobbler.com/2.0/' and not headers
        params = urllib.parse.parse_qsl(body, keep_blank_values=True, strict_parsing=True)
        assert params[-2:][1] == ('format', 'json') and params[-2][0] == 'api_sig'
        signed = sorted(params[:-2])
        assert [k for k, _ in params[:-2]] == [k for k, _ in signed]  # sent in signature order
        assert params[-2][1] == hashlib.md5((''.join(k + v for k, v in signed) + 'sec').encode()).hexdigest()
        p = dict(params)
        if p['method'] == 'auth.getMobileSession':
            assert (p['username'], p['password'], p['api_key']) == ('u', 'p&w', 'key'); continue
        assert p['method'] == 'track.scrobble' and p['sk'] == 'SK123'
        n = sum(k.startswith('artist[') for k in p)
        for j in range(n):
            i = int(p[f'timestamp[{j}]']) - 1700000000
            assert p[f'artist[{j}]'] == artist(i) and p[f'track[{j}]'] == f'Title {i}' and p[f'duration[{j}]'] == str(200 + i)
            assert p.get(f'album[{j}]') == ('Alb/um' if i % 10 else None)
        fm_sizes.append(n)
    assert lb_sizes == [50, 50, 20, 50, 10, 10] and fm_sizes == [50, 50, 20, 50]
    assert [v for _, _, v, _ in requests] == ['0'] * (len(requests) - 1) + ['1']
    print('Scrobble upload: config, batching, JSON and form escaping, Last.fm signatures, log rewrite and failures passed.')

def books_check(tmp):
    """Books (books.c): raw inflate against zlib and on garbage, XHTML to text, EPUB to text (stored
    and deflated entries, namespaced OPF, %XX hrefs, DRM, malformed zips), UTF-8 with the Latin-1
    fallback, and page layout forward and back."""
    import random, zipfile, zlib
    lib = compile_host(tmp, 'books.so', ROOT/'patch/books.c')
    lib.book_inflate.argtypes = [C.c_char_p, C.c_uint, C.c_char_p, C.c_uint]
    rng = random.Random(7)
    words = [bytes(rng.choice(b'abcdefghij ') for _ in range(rng.randrange(1, 9))) for _ in range(200)]
    samples = [b'', b'a', bytes(range(256)) * 40, b' '.join(rng.choice(words) for _ in range(30000)),
               bytes(rng.randrange(256) for _ in range(5000))]
    for data in samples:
        for level, strategy in ((0, 0), (1, 0), (6, 0), (9, 0), (6, zlib.Z_FIXED), (6, zlib.Z_HUFFMAN_ONLY), (6, zlib.Z_RLE)):
            c = zlib.compressobj(level, zlib.DEFLATED, -15, 9, strategy); packed = c.compress(data) + c.flush()
            out = C.create_string_buffer(len(data) + 1)
            assert lib.book_inflate(packed, len(packed), out, len(data)) == len(data) and out.raw[:len(data)] == data
            if data: assert lib.book_inflate(packed, len(packed), out, len(data) - 1) == -1  # longer than its room
            if data: assert lib.book_inflate(packed[:len(packed) // 2], len(packed) // 2, out, len(data)) == -1
    out = C.create_string_buffer(1 << 16)
    for _ in range(3000):  # garbage never reads or writes out of bounds, and ends
        junk = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 64)))
        assert -1 <= lib.book_inflate(junk, len(junk), out, 1 << 16) <= 1 << 16
    xhtml = lib.book_xhtml; xhtml.argtypes = [C.c_char_p]
    def text(s):
        b = C.create_string_buffer(s.encode()); n = xhtml(b); return b.raw[:n].decode()
    assert text('<?xml version="1.0"?><!DOCTYPE html><html><head><title>T</title><style>p{}</style></head>'
                '<body><h1>One</h1>\n  <p>A  <i>b</i>\tc&amp;d &lt;&#233;&#x1F600;&gt; &nbsp;e&bogus; &#xZZ;</p>'
                '<!-- <p>hidden</p> --><script>x<y</script><p>f<br/>g</p><div><p>h</p></div>&quot;&apos;</body></html>') == \
        'One\n\nA b c&d <\u00e9\U0001F600>  e&bogus; &#xZZ;\n\nf\ng\n\nh\n\n"\''
    assert text('<p>no end') == 'no end' and text('<head>never closed') == '' and text('&#1114112;&#0;<') == '&#1114112;&#0;'
    def epub(path, files, method=zipfile.ZIP_DEFLATED, container=True):
        with zipfile.ZipFile(path, 'w', method) as z:
            z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip')
            if container:
                z.writestr('META-INF/container.xml', '<?xml version="1.0"?><container><rootfiles>'
                           '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            for n, d in files.items(): z.writestr(n, d)
    opf = ('<opf:package><opf:manifest><opf:item id="c2" href="Text/two%20b.xhtml#top" media-type="application/xhtml+xml"/>'
           "<opf:item href='Text/one.xhtml' id='c1'/><opf:item id=\"img\" href=\"cover.jpg\" media-type=\"image/jpeg\"/><opf:item id=\"empty\" href=\"e.xhtml\"/>"
           '</opf:manifest><opf:spine><opf:itemref idref="img"/><opf:itemref idref="c1"/><opf:itemref idref="empty"/>'
           '<opf:itemref idref="missing"/><opf:itemref idref="c2"/></opf:spine></opf:package>')
    files = {'OEBPS/content.opf': opf, 'OEBPS/Text/one.xhtml': '<html><body><p>Hello caf\u00e9</p></body></html>' * 50,
             'OEBPS/Text/two b.xhtml': '<p>Second</p>', 'OEBPS/e.xhtml': '<html><head><title>x</title></head></html>',
             'OEBPS/cover.jpg': b'\xff\xd8\xff\xe0'}
    convert = lib.book_convert; convert.argtypes = [C.c_char_p, C.c_char_p, C.POINTER(C.c_int)]
    cancel = C.c_int(0); dst = tmp/'book.txt'
    def run(src): return convert(str(src).encode(), str(dst).encode(), C.byref(cancel))
    want = '\n\n'.join(['Hello caf\u00e9'] * 50) + '\f\nSecond'
    for method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
        epub(tmp/'a.epub', files, method); assert run(tmp/'a.epub') == 1 and dst.read_text() == want
    epub(tmp/'f.epub', {**files, 'META-INF/encryption.xml': '<CipherReference URI="OEBPS/Fonts/a.otf"/>'})
    assert run(tmp/'f.epub') == 1  # obfuscated fonts alone
    epub(tmp/'d.epub', {**files, 'META-INF/encryption.xml': '<CipherReference URI="OEBPS/Text/one.xhtml"/>'})
    assert run(tmp/'d.epub') == 0
    epub(tmp/'n.epub', files, container=False); assert run(tmp/'n.epub') == 0
    whole = (tmp/'a.epub').read_bytes()
    for cut in (0, 10, len(whole) // 2, len(whole) - 30):
        (tmp/'t.epub').write_bytes(whole[:cut]); assert run(tmp/'t.epub') == 0
    for _ in range(200):  # flipped bytes: a refusal or some text, never a crash
        b = bytearray(whole); b[rng.randrange(len(b))] ^= 1 << rng.randrange(8); (tmp/'x.epub').write_bytes(b); run(tmp/'x.epub')
    assert run(tmp/'none.epub') == 0
    cancel.value = 1; assert run(tmp/'a.epub') == 0; cancel.value = 0
    lib.book_char.argtypes = [C.c_char_p, C.c_uint, C.POINTER(C.c_uint)]
    def chars(b):
        i = C.c_uint(0); out = []
        while i.value < len(b): out.append(lib.book_char(b, len(b), C.byref(i)))
        return out
    assert chars('a\u00e9\u20ac\U0001F600'.encode()) == [97, 0xe9, 0x20ac, 0x1f600]
    assert chars(b'\xe9t\xc3') == [0xe9, ord('t'), 0xc3] and chars(b'\xe2\x82') == [0xe2, 0x82]
    MEASURE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint); LINE = C.CFUNCTYPE(None, C.c_void_p, C.c_int, C.POINTER(C.c_uint), C.c_int)
    lines = []
    measure = MEASURE(lambda ctx, c: 10); line = LINE(lambda ctx, row, s, n: lines.append((row, ''.join(map(chr, s[:n])))))
    lib.book_page.argtypes = [C.c_char_p, C.c_uint, C.c_uint, C.c_int, C.c_int, MEASURE, LINE, C.c_void_p]
    lib.book_back.argtypes = [C.c_char_p, C.c_uint, C.c_uint, C.c_int, C.c_int, MEASURE, C.c_void_p]
    def page(t, pos, rows=3, width=100):
        lines.clear(); return lib.book_page(t, len(t), pos, rows, width, measure, line, None), [l for _, l in lines]
    t = b'\n\xef\xbb\xbfThe quick brown fox\r\njumps over the lazy dog\n\nAndsuperlongwordhere end\fNext'
    # Wraps at the last space (a space at the edge is used up), mid-word without one; the BOM and
    # the top's line breaks are dropped; a \f ends the page; a page from it starts after it.
    assert page(t, 0, rows=9) == (t.index(b'\f'), ['The quick', 'brown fox', 'jumps over', 'the lazy', 'dog', '', 'Andsuperlo', 'ngwordhere', 'end'])
    assert page(t, t.index(b'\f'), rows=9) == (len(t), ['Next'])
    assert page(t, 0) == (t.index(b'the'), ['The quick', 'brown fox', 'jumps over'])
    # Back: in a text shorter than the look-back, exactly the page before; beyond it, a page that
    # reaches the one asked about.
    starts = [0]
    while starts[-1] < len(t): starts.append(page(t, starts[-1])[0])
    for a, b in zip(starts, starts[1:-1]): assert lib.book_back(t, len(t), b, 3, 100, measure, None) == a
    assert lib.book_back(t, len(t), 0, 3, 100, measure, None) == 0
    big = b'\n'.join(b' '.join(rng.choice(words) for _ in range(rng.randrange(1, 40))) for _ in range(800))
    pos = 0
    for _ in range(60):
        pos = page(big, pos, rows=10, width=320)[0]
        b = lib.book_back(big, len(big), pos, 10, 320, measure, None)
        assert b < pos and page(big, b, rows=10, width=320)[0] >= pos and pos - b < 3072
    print('Books: inflate, XHTML text, EPUB conversion and refusals, UTF-8 and page layout passed.')

def video_check(tmp):
    """Videos' player (video.c): ffmpeg's argv for the Q2's framebuffer, frame pacing and BT volume."""
    lib = compile_host(tmp, 'q2video.so', ROOT/'patch/video.c')
    def argv(at, audio):
        a, ss = (C.c_char_p * 27)(), C.create_string_buffer(16)
        lib.ffmpeg_argv(a, ss, at, b'/mnt/mmc/Videos/a b.mp4', audio)
        return [x.decode() for x in a[:a[:].index(None)]]
    # /dev/fb0 is 320x375 BGRA (display_logo, soc_fb.ko): fitted into the landscape 375x320 view and
    # turned clockwise, as the boot logo.
    head = ['/usr/bin/ffmpeg', '-nostdin', '-loglevel', 'quiet', '-ss', '30', '-i', '/mnt/mmc/Videos/a b.mp4',
            '-map', '0:v:0', '-vf', 'scale=375:320:force_original_aspect_ratio=decrease:flags=fast_bilinear,format=bgra,'
            'pad=375:320:(ow-iw)/2:(oh-ih)/2,transpose=clock', '-r', '25', '-f', 'rawvideo', 'pipe:3']
    assert argv(30, 1) == head + ['-map', '0:a:0', '-ac', '2', '-ar', '48000', '-f', 's16le', 'pipe:4']
    assert argv(30, 0) == head
    # Frame n shows from n/25 s of the clock, and is dropped a whole frame late.
    lib.frame_due.argtypes = [C.c_int, C.c_longlong]
    assert [lib.frame_due(1, t) for t in (0, 39, 40, 79, 80)] == [0, 0, 1, 1, 2] and lib.frame_due(0, 0) == 1
    # Bluetooth: hciplayer's soft volume curve in 1/65536, clamped to 0-100, and the samples scaled by it.
    assert [lib.bt_gain(v) for v in (-5, 0, 1, 25, 49, 50, 75, 99, 100, 120)] == \
        [0, 0, 131, 3276, 6422, 6553, 36044, 64356, 65536, 65536]
    pcm = (C.c_short * 6)(32767, -32768, 1000, -1000, 1, -1)
    lib.scale(pcm, 6, 65536); assert list(pcm) == [32767, -32768, 1000, -1000, 1, -1]
    lib.scale(pcm, 5, lib.bt_gain(50)); assert list(pcm) == [3276, -3277, 99, -100, 0, -1]
    lib.scale(pcm, 6, 0); assert list(pcm) == [0] * 6
    # The length from ffmpeg -i's report, for the position bar.
    assert lib.duration(b'Input #0, mov\n  Duration: 01:02:03.45, start: 0.0\n') == 3723
    assert lib.duration(b'  Duration: N/A, bitrate: N/A') == 0 and lib.duration(b'') == 0
    # The bar: along the bottom of the picture as seen, 40 px in from its ends and 23-30 px up, white
    # for the share given and black after; the picture is turned clockwise, so the bar runs down
    # columns 22-29 (picture x = panel y), from the top. Nothing else changes.
    def bar(n, total, line=1300):  # a padded line, as fix.line_length may be
        f = (C.c_ubyte * (line * 375))(*([0x55] * (line * 375)))
        lib.overlay(f, line, n, total)
        return [[f[y * line + x * 4 + b] for x in range(320) for b in range(4)] for y in range(375)], f
    p, f = bar(1, 4)
    for y in range(375):
        for x in range(320 * 4):
            want = 0x55 if not (22 * 4 <= x < 30 * 4 and 40 <= y < 335) else 0xff if y < 40 + 295 // 4 else 0
            assert p[y][x] == want, (x, y)
    assert all(f[y * 1300 + c] == 0x55 for y in range(375) for c in range(1280, 1300))
    assert [r[88] for r in bar(7, 5)[0][40:335]] == [0xff] * 295  # clamped to all
    assert [r[88] for r in bar(-3, 5)[0][40:335]] == [0] * 295 and bar(1, 0)[0][100][88] == 0x55
    print('Videos: ffmpeg argv, frame pacing, Bluetooth volume, length and bar passed.')

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='q2-peq-check-') as directory:
        tmp = pathlib.Path(directory); lib = library(tmp)
        parser_check(lib, tmp)
        dsp_check(lib)
        editor_check(lib, tmp)
        player_check(tmp)
        visualizer_check(tmp)
        scrobble_check(tmp)
        books_check(tmp)
        video_check(tmp)
