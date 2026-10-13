#include "peq.h"
#include "offsets.inc"

extern int stock_eq_trampoline(int mode);
/* All calls, including restored playback, instantiate the replacement even in bypass.
 * Afterwards the flag only drives the status-bar EQ icon (systembar_showface @0x52f8b0). */
int peq_stock_eq(int mode) {
    (void)mode;
    g_equalizer_flag = 1;
    int result = stock_eq_trampoline(1);
    peq_preset active;
    peq_load_active(&active);
    g_equalizer_flag = !active.bypass;
    return result;
}

/* PEQ off but its filter in the chain, for the visualizer's tap (visualizer.c): the switch's Off, so a
 * preset saved On while the stock flag is clear stays silent; nothing if that cannot be saved. */
void peq_attach(void) {
    peq_preset active;
    peq_load_active(&active);
    if (active.bypass || (active.bypass = 1, peq_save(PEQ_ACTIVE, &active, 1) == 1)) peq_stock_eq(1);
}

enum { HOME, BAND, PRESETS, IMPORTS, SAVES, CONFIRM, DELETES, PICK, ADJUST };
enum { BACK = 1000, APPLY, BYPASS, MENU, IMPORT, SAVE, ENABLE, TYPE, CHANNEL,
       YES, CANCEL, DELETE, BALANCE, PREAMP, GAIN, /* these three scrub picks[], in its order */
       FREQUENCY, QUALITY /* and these two scrub the band's own value */ };
#define GRAPH_W (375 - 2 * PEQ_GRAPH_X)
#if IPOD
#define ROW_X CF_EDGE /* the text column clear of the rounded glass, as coverflow.c's */
#define TONE accent_tone(2) /* the light tone */
#define TITLE_BAR 0 /* the status bar shows the title (navigation.c clock_sync) */
#else
#define TITLE_BAR 48
#define ROW_X 12
#define TONE 0xffffff
#endif
#define METER_W (375 - 2 * CF_EDGE)
static struct {
    void *page, *view, *graph, *value, *meter;
    unsigned timer;
    int screen, band, count, previous, rendered, dirty, pick, adjust, index;
    peq_preset draft, candidate, named; /* named: the preset applied under applied's name */
    char (*names)[256];
    char destination[600], status[160];
    char name[64], applied[64]; /* the draft's preset, and the active one's, when loaded from a file */
    float curve[2][GRAPH_W];      /* the left and right response, in graph rows */
} ui __attribute__((section(".scratch")));

/* Scrubbed values in 0.5 dB steps: index i is first + step * i. Preamp's index 0 is Auto, past its highest
 * value (first is unused there). */
static const struct { double first, step; int rows; } picks[] = {{-12, 0.5, 49}, {12.5, -0.5, 74}, {24, -0.5, 97}};
static int action(void *ctx, void *event);
static int render(const void *unused);
static int pick_index(void);
static void paint_meter(void *canvas);

/* A row: the caption on the left, white or, for display-only rows and off bands (dim), grey, and the
 * value, if any, in grey against the right edge. */
static void row_dim(void *view, int index, const char *text, const char *value, int id, int dim) {
    void *item = list_item_create(view, 0, index * 48, 375, 48);
    widget_use_style(item, "s_listitem_black");
    for (int i = 0; i < 1 + !!value; ++i) {
        void *label = label_create(item, ROW_X, 0, 375 - 2 * ROW_X, 48);
        widget_use_style(label, "s_label_white20l");
        if (i || dim || id < 0) widget_set_prop_int(label, "style:normal:text_color", (int)CF_GREY);
        if (i) widget_set_prop_str(label, "style:normal:text_align_h", "right");
        widget_set_text_utf8(label, i ? value : text);
    }
    widget_on(item, EVT_CLICK, action, (void *)(long)id);
}
#define row(view, index, text, id) row_dim(view, index, text, 0, id, 0)

/* |H|^2 of the engine's left and right chains at f Hz (at 48 kHz, as compiled here). */
static void response(const peq_engine *e, double f, double *l, double *r) {
    double w = 6.283185307179586 * f / 48000, c1 = cos(w), s1 = sin(w), c2 = 2 * c1 * c1 - 1, s2 = 2 * s1 * c1;
    *l = *r = 1;
    for (int i = 0; i < e->used; ++i) { /* bands past used are identity */
        const peq_coeff *q = &e->c[i];
        double nr = q->b0 + q->b1 * c1 + q->b2 * c2, ni = q->b1 * s1 + q->b2 * s2;
        double dr = 1 + q->a1 * c1 + q->a2 * c2, di = q->a1 * s1 + q->a2 * s2;
        double m = (nr * nr + ni * ni) / (dr * dr + di * di);
        if (e->only[i] != 2) *l *= m; /* only: 1 left, 2 right */
        if (e->only[i] != 1) *r *= m;
    }
}

static double decibels(double power) { return 10 * log(power) / 2.302585092994046; }
static float graph_y(double db) {
    if (db > PEQ_GRAPH_DB) db = PEQ_GRAPH_DB;
    if (db < -PEQ_GRAPH_DB) db = -PEQ_GRAPH_DB;
    return (float)((PEQ_PLOT_TOP + PEQ_PLOT_BOTTOM) / 2.0 - db * (PEQ_PLOT_BOTTOM - PEQ_PLOT_TOP) / (2.0 * PEQ_GRAPH_DB));
}
static float graph_x(double f) { return (float)(PEQ_GRAPH_X + (GRAPH_W - 1) * log(f / 20) / log(1000)); }

/* The draft's response, one point a column, for peq_paint. */
static void curve(void) {
    peq_engine e;
    int ok = peq_compile(&ui.draft, 48000, &e);
    for (int x = 0; x < GRAPH_W; ++x) {
        double l = 1, r = 1;
        if (ok) response(&e, 20 * pow(1000, x / (GRAPH_W - 1.0)), &l, &r);
        ui.curve[0][x] = graph_y(decibels(l));
        ui.curve[1][x] = graph_y(decibels(r));
    }
}

/* Preamp that keeps the combined response at or below 0 dB, as AutoEQ sets it: minus the peak
 * of |H| on a log grid from 20 Hz to 20 kHz at 48 kHz. ponytail: 1024 points (0.7% apart); a very
 * narrow boost between two can read a little low, refine around the best point if that matters. */
static double headroom(const peq_preset *p) {
    peq_engine e;
    if (!peq_compile(p, 48000, &e)) return p->preamp; /* preamp only sets e.gain, unused here */
    double peak = 1;
    for (int k = 0; k < 1024; ++k) {
        double l, r;
        response(&e, 20 * pow(1000, k / 1023.0), &l, &r);
        if (l > peak) peak = l;
        if (r > peak) peak = r;
    }
    double gain = -decibels(peak);
    return peak == 1 ? 0 : gain < -60 ? -60 : gain; /* no boost: +0, not -0.0 on screen */
}

static double *picked(void) {
    return (double *[]){&ui.draft.balance, &ui.draft.preamp, &ui.draft.bands[ui.band].gain}[ui.pick - BALANCE];
}

static int on_auto(void) { return __builtin_fabs(ui.draft.preamp - headroom(&ui.draft)) < 0.05; }

static int compare_names(const void *a, const void *b) { return strcmp(a, b); }

static void list_files(const char *folder, const char *extension) {
    ui.count = 0;
    if (!ui.names) ui.names = calloc(256, 256);
    if (!ui.names) { snprintf(ui.status, sizeof(ui.status), "Out of memory"); return; }
    void *dir = opendir(folder);
    if (!dir) { snprintf(ui.status, sizeof(ui.status), "Folder missing or unavailable"); return; }
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        const char *name = entry->d_name;
        unsigned n = strlen(name), e = strlen(extension);
        if (entry->d_type != 8 && entry->d_type != 0) continue;
        if (n <= e || n >= 256 || strcmp(name + n - e, extension)) continue;
        if (ui.count == 256) break; /* show the first 256 */
        memcpy(ui.names[ui.count++], name, n + 1);
    }
    closedir(dir);
    qsort(ui.names, ui.count, 256, compare_names);
    if (!ui.count && !ui.status[0]) snprintf(ui.status, sizeof(ui.status), "No presets found");
}

/* File name in ui.destination: sizeof skips PEQ_SAVED and its "/". */
static const char *deleting(void) { return ui.destination + sizeof(PEQ_SAVED); }

static void save_candidate(int replace) {
    mkdir(PEQ_SAVED, 0700);
    int result = peq_save(ui.destination, &ui.candidate, replace);
    if (result == 2) { ui.previous = ui.screen; ui.screen = CONFIRM; }
    else snprintf(ui.status, sizeof(ui.status), result ? "Preset saved; active EQ unchanged" : "Save failed; settings unchanged");
}

static int action(void *ctx, void *event) {
    (void)event;
    int id = (int)(long)ctx;
    if (id < 0) return 0; /* display-only rows */
    peq_band *b = &ui.draft.bands[ui.band];
    ui.status[0] = 0;
    /* Band edits keep the preamp on Auto (just enough cut for the boosts) only if it was there. */
    int edit = id >= ENABLE && id <= CHANNEL;
    int automatic = edit && on_auto();
    if (id == BACK) {
        if (ui.screen == HOME) { navigator_back(); return 0; }
        if (ui.screen == PICK) ui.screen = ui.pick == GAIN ? BAND : HOME;
        else if (ui.screen == ADJUST) ui.screen = BAND;
        else ui.screen = ui.screen == BAND || ui.screen == PRESETS ? HOME : PRESETS;
    } else if (id == MENU) ui.screen = PRESETS;
    else if (id == IMPORT) ui.screen = IMPORTS;
    else if (id == DELETE) ui.screen = DELETES;
    else if (id >= BALANCE && id <= GAIN) { ui.pick = id; ui.screen = PICK; ui.index = pick_index(); }
    else if (id == FREQUENCY || id == QUALITY) { ui.adjust = id; ui.screen = ADJUST; }
    else if (id == SAVE) ui.screen = SAVES;
    else if (id == BYPASS) {
        /* Takes effect at once and keeps unapplied band edits out of the active preset. */
        peq_preset active;
        peq_load_active(&active);
        active.bypass = !ui.draft.bypass;
        if (peq_save(PEQ_ACTIVE, &active, 1) == 1) {
            ui.draft.bypass = active.bypass;
            /* The stock switch's config key: boot instantiates the filter only when it is set. */
            write_int_config(!active.bypass, "PLAYSET", "EQFLAG");
            peq_stock_eq(1);
        } else snprintf(ui.status, sizeof(ui.status), "Switch failed; PEQ unchanged");
    }
    else if (id == ENABLE) b->enabled = !b->enabled;
    else if (id == TYPE) b->type = (b->type + 1) % 3;
    else if (id == CHANNEL && b->enabled) b->enabled = b->enabled % 3 + 1; /* both, left, right */
    else if (id == APPLY) {
        if (peq_save(PEQ_ACTIVE, &ui.draft, 1) == 1) {
            peq_stock_eq(1);
            ui.dirty = 0;
            memcpy(ui.applied, ui.name, sizeof(ui.name));
            ui.named = ui.draft;
            snprintf(ui.status, sizeof(ui.status), "Applied");
        } else snprintf(ui.status, sizeof(ui.status), "Apply failed; active EQ unchanged");
    } else if (id == YES) {
        ui.screen = ui.previous;
        if (ui.screen != DELETES) save_candidate(1);
        /* Saved presets are copies; the active EQ and the editor draft are untouched. */
        else if (unlink(ui.destination)) snprintf(ui.status, sizeof(ui.status), "Delete failed; preset kept");
        else snprintf(ui.status, sizeof(ui.status), "Deleted %.100s; active EQ unchanged", deleting());
    } else if (id == CANCEL) ui.screen = ui.previous;
    else if (id < 256) {
        if (ui.screen == HOME && id < PEQ_BANDS) { ui.band = id; ui.screen = BAND; }
        else if (ui.screen == SAVES && id < 10) {
            snprintf(ui.destination, sizeof(ui.destination), PEQ_SAVED "/Manual %02d.peq", id + 1);
            ui.candidate = ui.draft;
            save_candidate(0);
        } else if (id < ui.count && ui.screen == IMPORTS) {
            char path[600];
            peq_error error;
            snprintf(path, sizeof(path), PEQ_IMPORT "/%s", ui.names[id]);
            if (!peq_import_file(path, &ui.candidate, &error))
                snprintf(ui.status, sizeof(ui.status), "Line %u: %s", error.line, error.reason);
            else {
                unsigned n = strlen(ui.names[id]) - 4;
                snprintf(ui.destination, sizeof(ui.destination), PEQ_SAVED "/%.*s.peq", (int)n, ui.names[id]);
                save_candidate(0);
            }
        } else if (id < ui.count && ui.screen == DELETES) {
            snprintf(ui.destination, sizeof(ui.destination), PEQ_SAVED "/%s", ui.names[id]);
            ui.previous = DELETES;
            ui.screen = CONFIRM;
        } else if (id < ui.count && ui.screen == PRESETS) {
            char path[600];
            snprintf(path, sizeof(path), PEQ_SAVED "/%s", ui.names[id]);
            peq_preset p;
            if (peq_load(path, &p)) {
                /* Loading into the editor is deliberate; Apply is the activation step. */
                p.bypass = ui.draft.bypass; /* ON/OFF is live state, not part of the edit */
                ui.draft = p;
                ui.dirty = 1;
                snprintf(ui.name, sizeof(ui.name), "%.*s", (int)strlen(ui.names[id]) - 4, ui.names[id]);
                ui.screen = HOME;
                snprintf(ui.status, sizeof(ui.status), "Preset loaded; choose Apply to activate");
            } else snprintf(ui.status, sizeof(ui.status), "Cannot read preset; settings unchanged");
        }
    }
    if (edit) { /* ENABLE, TYPE and CHANNEL */
        ui.dirty = 1;
        if (automatic) ui.draft.preamp = headroom(&ui.draft);
    }
    if (!ui.timer) ui.timer = timer_add(render, 0, 1);
    return 0;
}

/* Where the scrubbed value sits in its range, 0 to 1 with higher values to the right, and in *origin
 * where its fill starts: 0 dB for gain and balance, else the left end. Preamp's Auto is the right end. */
static double meter_at(double *origin) {
    peq_band *b = &ui.draft.bands[ui.band];
    *origin = 0;
    if (ui.screen == ADJUST)
        return ui.adjust == QUALITY ? log(b->q / 0.1) / log(100) : log(b->frequency / 20) / log(1000);
    double first = picks[ui.pick - BALANCE].first, step = picks[ui.pick - BALANCE].step;
    int last = picks[ui.pick - BALANCE].rows - 1;
    if (ui.pick != PREAMP) *origin = step > 0 ? -first / step / last : 1 + first / step / last;
    return step > 0 ? (double)ui.index / last : (double)(last - ui.index) / last;
}

static void value_text(char *s, unsigned n) {
    peq_band *b = &ui.draft.bands[ui.band];
    double v = ui.draft.balance;
    if (ui.screen == ADJUST)
        snprintf(s, n, ui.adjust == QUALITY ? "%.2f" : "%.0f Hz", ui.adjust == QUALITY ? b->q : b->frequency);
    else if (ui.pick == PREAMP && !ui.index) snprintf(s, n, "Auto %.1f dB", ui.draft.preamp);
    else if (ui.pick == BALANCE) /* Balance turns one side down: R 1.0 dB is the left 1 dB quieter. */
        snprintf(s, n, __builtin_fabs(v) >= 0.05 ? "%s %.1f dB" : "Centre", v > 0 ? "R" : "L", __builtin_fabs(v));
    else snprintf(s, n, "%+.1f dB", *picked());
}

/* The picks[] index nearest the current value (Auto when the preamp is on it). */
static int pick_index(void) {
    int last = picks[ui.pick - BALANCE].rows - 1;
    double r = (*picked() - picks[ui.pick - BALANCE].first) / picks[ui.pick - BALANCE].step + 0.5;
    int i = r < 0 ? 0 : r > last ? last : (int)r;
    if (ui.pick == PREAMP && on_auto()) return 0;
    return ui.pick == PREAMP && !i ? 1 : i;
}

static int render(const void *unused) {
    (void)unused;
    ui.timer = 0;
    if (!ui.page) return 7;
    int selection = 0, offset = 0;
    int scrub = ui.screen == PICK || ui.screen == ADJUST;
    int height = widget_get_prop_int(ui.page, "h", 320), graph = ui.screen == HOME || ui.screen == BAND || scrub;
    /* iPod shows the title in the status bar, so the bar here holds only a message. */
    int head = ui.status[0] ? 48 : TITLE_BAR, top = head + graph * PEQ_GRAPH_H;
    /* Whole rows only, so the last row is never clipped. */
    int rows = (height - top) / 48 * 48;
    if (ui.view && ui.rendered == ui.screen) {
        selection = widget_get_prop_int(ui.view, "_ringnav_index", 0);
        offset = widget_get_prop_int(ui.view, "yoffset", 0);
    }
    ui.rendered = ui.screen;
    widget_destroy_children(ui.page);
    ui.view = ui.value = ui.meter = 0;
    char text[160], value[32];
    if (scrub) {
        /* The value large under the live curve, with a slim bar for its place in the range. The
         * wheel scrubs it (peq_key); Centre or Return finishes. */
        ui.value = label_create(ui.page, CF_EDGE, top + 24, METER_W, 56);
        widget_use_style(ui.value, "s_label_white20c");
        widget_set_prop_int(ui.value, "style:normal:font_size", 40);
        value_text(value, sizeof(value));
        widget_set_text_utf8(ui.value, value);
        ui.meter = widget_factory_create_widget(widget_factory(), "view", ui.page, CF_EDGE, top + 92, METER_W, 20);
    } else {
        void *list = list_view_create(ui.page, 0, top, 375, rows);
        widget_set_prop_int(list, "item_height", 48);
        /* The theme's default list_view is a light card; stock pages paint theirs black inline. */
        widget_set_prop_int(list, "style:normal:bg_color", (int)0xff000000u);
        widget_set_prop_int(list, "style:normal:border_color", 0);
        void *view = scroll_view_create(list, 0, 0, 375, rows);
        ui.view = view;
        widget_set_prop_int(view, "yslidable", 1);
        widget_set_prop_int(view, "xslidable", 0);
        widget_set_prop_int(view, "_ringnav_index", selection);
        int n = 0;
        static const char *const types[] = {"Peaking", "Low shelf", "High shelf"};
        /* No Back row: Return steps back on every screen (keyup). */
        if (ui.screen == HOME) {
            row(view, n++, ui.dirty ? "Apply changes" : "Nothing to apply", ui.dirty ? APPLY : -1);
            row_dim(view, n++, "PEQ", ui.draft.bypass ? "Off" : "On", BYPASS, 0);
            snprintf(value, sizeof(value), on_auto() ? "%.1f dB (Auto)" : "%.1f dB", ui.draft.preamp);
            row_dim(view, n++, "Preamp", value, PREAMP, 0);
            /* Balance turns one side down: R 1.0 dB is the left 1 dB quieter. */
            snprintf(value, sizeof(value), __builtin_fabs(ui.draft.balance) >= 0.05 ? "%s %.1f dB" : "Centre",
                     ui.draft.balance > 0 ? "R" : "L", __builtin_fabs(ui.draft.balance));
            row_dim(view, n++, "Balance", value, BALANCE, 0);
            row(view, n++, "Presets", MENU);
            /* Ten rows, then one spare past the last used band to add another. */
            for (int i = 0; i < PEQ_BANDS && (i < 10 || i <= ui.draft.count); ++i) {
                peq_band *b = &ui.draft.bands[i];
                snprintf(text, sizeof(text), "%d  %s %.0f Hz%s", i + 1, types[b->type], b->frequency,
                         (const char *[]){"", "", " L", " R"}[b->enabled]);
                snprintf(value, sizeof(value), b->enabled ? "%+.1f dB" : "Off", b->gain);
                row_dim(view, n++, text, value, i, !b->enabled);
            }
        } else if (ui.screen == BAND) {
            peq_band *b = &ui.draft.bands[ui.band];
            /* Imported missing bands already have valid defaults from peq_default. */
            if (ui.draft.count <= ui.band) ui.draft.count = ui.band + 1;
            row_dim(view, n++, "Band", b->enabled ? "On" : "Off", ENABLE, 0);
            if (b->enabled) row_dim(view, n++, "Channels", (const char *[]){0, "Both", "Left", "Right"}[b->enabled], CHANNEL, 0);
            row_dim(view, n++, "Type", types[b->type], TYPE, 0);
            snprintf(value, sizeof(value), "%.0f Hz", b->frequency); row_dim(view, n++, "Frequency", value, FREQUENCY, 0);
            snprintf(value, sizeof(value), "%+.1f dB", b->gain); row_dim(view, n++, "Gain", value, GAIN, 0);
            snprintf(value, sizeof(value), "%.2f", b->q); row_dim(view, n++, "Q", value, QUALITY, 0);
        } else if (ui.screen == CONFIRM && ui.previous == DELETES) {
            snprintf(text, sizeof(text), "Delete %.100s? Confirm", deleting()); row(view, n++, text, YES);
            row(view, n++, "Cancel", CANCEL);
        } else if (ui.screen == CONFIRM) {
            row(view, n++, "Replace existing preset? Confirm", YES);
            row(view, n++, "Cancel replacement", CANCEL);
        } else if (ui.screen == SAVES) {
            for (int i = 0; i < 10; ++i) {
                snprintf(text, sizeof(text), "Save as Manual %02d", i+1); row(view, n++, text, i);
            }
        } else {
            if (ui.screen == PRESETS) {
                row(view, n++, "Import from SD /EQ", IMPORT);
                row(view, n++, "Save editor preset", SAVE);
                row(view, n++, "Delete a preset", DELETE);
            }
            list_files(ui.screen == IMPORTS ? PEQ_IMPORT : PEQ_SAVED, ui.screen == IMPORTS ? ".txt" : ".peq");
            for (int i = 0; i < ui.count; ++i) row(view, n++, ui.names[i], i);
        }
        widget_set_prop_int(view, "virtual_h", n * 48);
        if (n * 48 < rows) { /* short lists: shrink so the list's white background never shows below the rows */
            widget_resize(list, 375, n * 48);
            widget_resize(view, 375, n * 48);
        }
        scroll_view_set_offset(view, 0, offset);
    }
    ui.graph = 0;
    if (graph) {
        curve();
        ui.graph = widget_factory_create_widget(widget_factory(), "view", ui.page, 0, head, 375, PEQ_GRAPH_H);
    }
    /* A message takes the title bar until the next action: on Stock in place of the title, so it never
     * shrinks the list; on iPod, whose title is in the status bar, a bar that shows only meanwhile. */
    void *title = label_create(ui.page, 8, 0, 359, 48);
    widget_use_style(title, "s_label_white20c");
    widget_set_prop_int(title, "line_wrap", 1);
    widget_set_name(title, "page_title");
    if (!TITLE_BAR) {
        widget_set_visible(title, 0, 0);
        if (ui.status[0]) {
            void *message = label_create(ui.page, 8, 0, 359, 48);
            widget_use_style(message, "s_label_white20c");
            widget_set_prop_int(message, "line_wrap", 1);
            widget_set_text_utf8(message, ui.status);
        }
    }
    if (TITLE_BAR && ui.status[0]) snprintf(text, sizeof(text), "%s", ui.status);
    else if (ui.screen == BAND) snprintf(text, sizeof(text), "PEQ Band %d", ui.band + 1);
    else if (ui.screen == ADJUST) snprintf(text, sizeof(text), ui.adjust == QUALITY ? "PEQ Band %d Q" : "PEQ Band %d Frequency (Hz)", ui.band + 1);
    else if (ui.screen == PICK && ui.pick == GAIN) snprintf(text, sizeof(text), "PEQ Band %d Gain", ui.band + 1);
    else if (ui.screen == PICK) snprintf(text, sizeof(text), ui.pick == PREAMP ? "PEQ Preamp" : "PEQ Balance");
    else if (ui.name[0]) snprintf(text, sizeof(text), "PEQ: %s", ui.name);
    else snprintf(text, sizeof(text), "PEQ");
    widget_set_text_utf8(title, text);
    widget_invalidate_force(ui.page, 0);
    return 7; /* RET_REMOVE */
}

/* 0xRRGGBB and an alpha to a color_t, whose bytes are r, g, b, a. Shared with visualizer.c. */
unsigned rgba(unsigned c, unsigned a) { return a << 24 | (c & 255) << 16 | (c & 0xff00) | c >> 16; }

/* ASCII s centred in x, y, w, h (draw_centred). Shared with visualizer.c. */
void caption(void *canvas, const char *s, int x, int y, int w, int h, unsigned px, unsigned color) {
    unsigned wide[16], n = 0;
    for (; s[n] && n < 16; ++n) wide[n] = (unsigned char)s[n];
    int r[4] = {x, y, w, h};
    draw_centred(canvas, wide, n, r, px, color);
}

static void path(void *vg, const float *y) {
    vgcanvas_begin_path(vg);
    vgcanvas_move_to(vg, PEQ_GRAPH_X, y[0]);
    for (int x = 1; x < GRAPH_W; ++x) vgcanvas_line_to(vg, PEQ_GRAPH_X + x, y[x]);
}

/* The graph view (ringnav_paint, after stock): a faint grid at 0, +-6 and +-12 dB and 100 Hz, 1 kHz
 * and 10 kHz, labelled in grey; the response as a 2px line in the accent's light tone (white in
 * Stock), fading to clear toward 0 dB, the other channel's dimmer when the two differ; a ring on each
 * band in use, the edited band's larger and filled. Off: the line alone, grey. */
void peq_paint(void *w, void *canvas) {
    if (w && w == ui.meter && P(canvas, CANVAS_LCD)) {
        unsigned fill = (unsigned)I(P(canvas, CANVAS_LCD), LCD_FILL_COLOR);
        paint_meter(canvas);
        canvas_set_fill_color(canvas, fill);
        return;
    }
    if (!w || w != ui.graph || !P(canvas, CANVAS_LCD)) return;
    static const int lines[] = {0, 6, -6, 12, -12}, ticks[] = {100, 1000, 10000};
    static const char *const labels[] = {"100", "1k", "10k"};
    unsigned fill = (unsigned)I(P(canvas, CANVAS_LCD), LCD_FILL_COLOR);
    unsigned tone = ui.draft.bypass ? 0x666666 : TONE;
    float zero = graph_y(0);
    for (int i = 0; i < 5; ++i) {
        canvas_set_fill_color(canvas, rgba(i ? 0x262626 : 0x4a4a4a, 255));
        canvas_fill_rect(canvas, PEQ_GRAPH_X, (int)graph_y(lines[i]), GRAPH_W, 1);
    }
    for (int i = 0; i < 3; ++i) {
        int x = (int)graph_x(ticks[i]);
        canvas_set_fill_color(canvas, rgba(0x262626, 255));
        canvas_fill_rect(canvas, x, PEQ_PLOT_TOP, 1, PEQ_PLOT_BOTTOM - PEQ_PLOT_TOP);
        caption(canvas, labels[i], x - 16, PEQ_PLOT_BOTTOM + 2, 32, PEQ_GRAPH_H - PEQ_PLOT_BOTTOM - 2, 12, CF_GREY);
    }
    canvas_set_fill_color(canvas, fill);
    void *vg = canvas_get_vgcanvas(canvas);
    if (!vg) return;
    int split = 0;
    for (int x = 0; x < GRAPH_W; ++x) split |= __builtin_fabsf(ui.curve[0][x] - ui.curve[1][x]) > 0.25f;
    vgcanvas_save(vg);
    vgcanvas_translate(vg, (float)I(canvas, CANVAS_X), (float)I(canvas, CANVAS_Y));
    if (!ui.draft.bypass) /* one path, two fills: each gradient is clear beyond 0 dB on its side */
        for (int side = 0; side < 2; ++side) {
            path(vg, ui.curve[0]);
            vgcanvas_line_to(vg, PEQ_GRAPH_X + GRAPH_W - 1, zero);
            vgcanvas_line_to(vg, PEQ_GRAPH_X, zero);
            vgcanvas_close_path(vg);
            vgcanvas_set_fill_linear_gradient(vg, 0, side ? PEQ_PLOT_BOTTOM : PEQ_PLOT_TOP, 0, zero,
                                              rgba(tone, 0x90), rgba(tone, 0));
            vgcanvas_fill(vg);
        }
    vgcanvas_set_line_width(vg, 2);
    for (int ch = split; ch >= 0; --ch) {
        path(vg, ui.curve[ch]);
        vgcanvas_set_stroke_color(vg, rgba(tone, ch ? 0x80 : 0xff));
        vgcanvas_stroke(vg);
    }
    for (int i = 0; i < ui.draft.count; ++i) {
        const peq_band *b = &ui.draft.bands[i];
        int edited = i == ui.band && (ui.screen == BAND || ui.screen == ADJUST ||
                                      (ui.screen == PICK && ui.pick == GAIN));
        if (!b->enabled && !edited) continue;
        vgcanvas_begin_path(vg);
        vgcanvas_arc(vg, graph_x(b->frequency), graph_y(b->gain), edited ? 5 : 3.5f, 0, 6.2831853f, 0);
        vgcanvas_set_fill_color(vg, rgba(edited ? tone : 0, 255));
        vgcanvas_fill(vg);
        vgcanvas_set_stroke_color(vg, rgba(edited ? 0xffffff : tone, 255));
        vgcanvas_stroke(vg);
    }
    vgcanvas_restore(vg);
}

/* n wheel steps on the value screen, applied live: 0.5 dB for gain, preamp and balance (preamp's Auto
 * past its highest value), 1/48 octave for frequency and 1/24 for Q, at least 1 Hz or 0.01. Band edits
 * keep the preamp on Auto only if it was there; scrubbing a gain turns its band on. */
static void scrub(int n) {
    peq_band *b = &ui.draft.bands[ui.band];
    int automatic = (ui.screen == ADJUST || ui.pick == GAIN) && on_auto();
    if (ui.screen == ADJUST) {
        int k = ui.adjust == QUALITY;
        double *v = k ? &b->q : &b->frequency, unit = k ? 0.01 : 1;
        double next = (double)(long)(*v * pow(2, n / (k ? 24.0 : 48.0)) / unit + 0.5) * unit;
        if (next == *v) next += n > 0 ? unit : -unit;
        *v = next < (k ? 0.1 : 20) ? (k ? 0.1 : 20) : next > (k ? 10 : 20000) ? (k ? 10 : 20000) : next;
    } else {
        int last = picks[ui.pick - BALANCE].rows - 1;
        int i = ui.index + (picks[ui.pick - BALANCE].step > 0 ? n : -n);
        ui.index = i < 0 ? 0 : i > last ? last : i;
        double first = picks[ui.pick - BALANCE].first, step = picks[ui.pick - BALANCE].step;
        *picked() = ui.pick == PREAMP && !ui.index ? headroom(&ui.draft) : first + step * ui.index;
        if (ui.pick == GAIN && !b->enabled) b->enabled = 1;
    }
    ui.dirty = 1;
    if (automatic) ui.draft.preamp = headroom(&ui.draft);
    char value[32];
    value_text(value, sizeof(value));
    widget_set_text_utf8(ui.value, value);
    curve();
    widget_invalidate_force(ui.page, 0);
}

/* Whether top is the PEQ page on its value screen, which takes the wheel and Centre (peq_key). */
int peq_scrubbing(void *top) { return ui.page && top == ui.page && ui.value; }

/* ringnav(): the value screen's wheel, steps already scaled by its spin, and Centre, which finishes as
 * Return does. Returns whether it took the key. */
int peq_key(void *top, unsigned key, int steps) {
    if (!peq_scrubbing(top)) return 0;
    if (key == KEY_CENTER) action((void *)(long)BACK, 0);
    else if (key == KEY_NEXT || key == KEY_PREV) scrub(key == KEY_NEXT ? steps : -steps);
    else return 0;
    return 1;
}

/* The value screen's bar: a dark track, the accent fill from its origin to the value, a white mark. */
static void paint_meter(void *canvas) {
    double origin, at = meter_at(&origin);
    at = at < 0 ? 0 : at > 1 ? 1 : at;
    int w = METER_W - 4, x = 2 + (int)(at * w), o = 2 + (int)(origin * w), cy = 10;
    canvas_set_fill_color(canvas, rgba(TRACK_COLOR, 255));
    canvas_fill_rect(canvas, 0, cy - 2, METER_W, 4);
    canvas_set_fill_color(canvas, rgba(ui.draft.bypass ? 0x666666 : TONE, 255));
    canvas_fill_rect(canvas, x < o ? x : o, cy - 2, x < o ? o - x : x - o, 4);
    canvas_set_fill_color(canvas, rgba(0xffffff, 255));
    canvas_fill_rect(canvas, x - 2, cy - 8, 4, 16);
}

/* Replaces the stock page's on_common_keyup: Return steps back one screen. */
static int keyup(void *ctx, void *event) {
    if (I(event, EVENT_KEY) != KEY_RETURN) return 0;
    action((void *)(long)BACK, ctx);
    return 11; /* RET_STOP */
}

static int closed(void *ctx, void *event) {
    (void)ctx; (void)event;
    if (ui.timer) timer_remove(ui.timer);
    ui.timer = 0;
    ui.page = 0;
    ui.view = ui.value = ui.meter = 0;
    ui.graph = 0;
    free(ui.names);
    ui.names = 0;
    return 0;
}

int peq_page_init(void *page, void *context) {
    (void)context;
    if (!page) return 16;
    ui.page = page;
    ui.view = 0;
    ui.screen = HOME;
    ui.status[0] = 0;
    ui.dirty = 0;
    peq_load_active(&ui.draft);
    /* Boot instantiates the filter only when the switch left PLAYSET/EQFLAG set; the flag
     * then follows the active preset's bypass (peq_stock_eq). */
    ui.draft.bypass = ui.named.bypass = !g_equalizer_flag;
    if (memcmp(&ui.draft, &ui.named, sizeof(ui.named))) ui.applied[0] = 0; /* replaced since */
    memcpy(ui.name, ui.applied, sizeof(ui.name));
    widget_on(page, EVT_DESTROY, closed, 0);
    widget_on(page, EVT_KEY_UP, keyup, 0);
    render(0);
    return 0;
}
