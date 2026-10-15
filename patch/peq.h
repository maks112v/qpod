#ifndef Q2_PEQ_H
#define Q2_PEQ_H

#ifdef PEQ_HOST
#ifndef _GNU_SOURCE
#define _GNU_SOURCE /* mmap64 */
#endif
#include <fcntl.h>
#include <sys/mman.h>
#include <time.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <unistd.h>
#else
#include "peq_platform.h"
#define PEQ_ROOT "" /* host tests pass a scratch directory */
#endif

#define PEQ_BANDS 30 /* 10 per side plus 10 shared, as L/R APO files use */
#define PEQ_CHANNELS 8
#define PEQ_FILE_LIMIT 16384
#define PEQ_LINE_LIMIT 512
#define PEQ_ACTIVE PEQ_ROOT "/mnt/data/peq-active"
#define PEQ_SAVED PEQ_ROOT "/mnt/data/peq-presets"
#define PEQ_IMPORT PEQ_ROOT "/mnt/mmc/EQ"

/* Versioned disk representation: fixed-width fields, no pointers or implicit padding.
 * enabled: 0 off, 1 both channels, 2 left only, 3 right only (v1 files hold 0 or 1).
 * balance: dB, above 0 turns the left channel down, below 0 the right (v2 only). */
typedef struct { int enabled, type; double frequency, gain, q; } peq_band;
typedef struct { int count, bypass; double preamp; peq_band bands[PEQ_BANDS]; double balance; } peq_preset;
typedef struct { unsigned line; const char *reason; } peq_error;
typedef struct { double b0, b1, b2, a1, a2; } peq_coeff;
typedef struct {
    peq_coeff c[PEQ_BANDS];
    double z[PEQ_CHANNELS][PEQ_BANDS][2], gain[PEQ_CHANNELS];
    int bypass, used, only[PEQ_BANDS]; /* used: bands past it are identity with zero memory; only: 0 every channel, else channel only-1 */
} peq_engine;
typedef struct {
    peq_engine current, next, pending;
    int rate, channels, ramp, ramp_length, waiting;
} peq_dsp;

void peq_default(peq_preset *p);
int peq_valid(const peq_preset *p);
int peq_parse(const char *text, unsigned size, peq_preset *out, peq_error *error);
int peq_number(const char *s, double *out);
int peq_import_file(const char *path, peq_preset *out, peq_error *error);
int peq_load(const char *path, peq_preset *out);
void peq_load_active(peq_preset *p);
int peq_save(const char *path, const peq_preset *p, int replace);
int peq_compile(const peq_preset *p, int rate, peq_engine *out);
void peq_reset(peq_dsp *d, int rate, int channels, const peq_preset *p);
int peq_update(peq_dsp *d, const peq_preset *p);
void peq_process(peq_dsp *d, float *audio, unsigned frames);

/* The visualizer's PCM tap (docs/internals.md#visualizer): hciplayer's filter writes what plays, its
 * first two channels and above 48 kHz every rate / 44100th frame, to ring[seq % VIS_RING], then sets
 * rate (of the ring), stamp (CLOCK_MONOTONIC ns of that write) and seq, the frames written. demo
 * creates the file, sized, under another name and renames it, so it never maps a short one, and bumps
 * want each frame it shows: a second of audio without a bump and the writer stops copying. */
#define VIS_FILE PEQ_ROOT "/tmp/q2vis"
#define VIS_RING 65536 /* a power of two: 1.5 s at 44.1 kHz, room for VIS_LATENCY_MS */
typedef struct { unsigned seq, rate; long long stamp; unsigned want, pad; float ring[VIS_RING][2]; } vis_tap;
long long now_ns(void);

/* Audited against hciplayer 9c3f8c6d… and MPlayer 1.3.0 libaf/af.h: the filter peq_player.c opens. */
typedef struct { void *audio; int len, rate, nch, format, bps; } af_data;
typedef struct af_instance {
    const void *info;
    int (*control)(struct af_instance *, int, void *);
    void (*uninit)(struct af_instance *);
    af_data *(*play)(struct af_instance *, af_data *);
    void *setup;
    af_data *data;
    struct af_instance *next, *prev;
    double delay, mul;
} af_instance;
typedef struct { peq_dsp dsp; peq_preset preset; } player_state;

/* The payload's shared helpers: coverflow.c's pages, hashes and worker, photos.c's, navigation.c's. */
void *text(void *parent, int x, int y, int w, int h);
void *page_open(const char *name, int (*closed)(void *, void *), int (*keyup)(void *, void *));
void *page_title(void *body, const char *caption);
void page_empty(void *title, int h);
void *page_list(void *page, void *body, void **title, const char *caption, int n, int item_h);
void page_row_detail(void *view, int index, const char *caption, const char *detail,
                     int (*click)(void *, void *));
void *bottom_caption(void *parent, int h);
int visual(void *s, int n, int *c, int *frac);
int thumb(const char *src, const char *dst, int w, int h);
int card_space(const char *dir);
int by_string(const void *a, const void *b);
unsigned hash_bytes(unsigned h, const unsigned char *s, unsigned n);
unsigned fnv(unsigned h, const unsigned char *s);
void stop_timer(unsigned *timer), rearm(unsigned *timer, int (*fn)(const void *), unsigned ms);
int worker_stop(unsigned long thread, int *running, volatile int *cancel, unsigned *timer);
void ringnav_select(void *w, int id, int rows);
void blob_io(const char *path, const char *tmp, void *buf, unsigned size, int write);
#define BLOB_IO(file, buf, write) blob_io(file, file ".tmp", &(buf), sizeof(buf), write)
int clip_within(void *canvas, int *old, int *clip, int x, int y, int w, int h);
void play_folder(void *dq, int idx);
void draw_centred(void *canvas, const unsigned *s, unsigned n, const void *r, unsigned px, unsigned color);
unsigned accent_tone(int tone); /* iPod: the Accent's ACCENTS column, 0xRRGGBB */
unsigned rgba(unsigned rgb, unsigned alpha), mix(unsigned from, unsigned to, int j, int n);
void caption(void *canvas, const char *s, int x, int y, int w, int h, unsigned px, unsigned color);
void peq_paint(void *w, void *canvas);

#endif
