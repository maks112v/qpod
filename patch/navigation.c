/* Logical menu selection is independent of native touch focus. Stock code owns gestures. */
#include "offsets.inc"
#include "peq.h" /* libc/libcstl imports (deque_*, send) and the shared helpers */
#include "stock.h"
extern int stock_keyup_trampoline(void *, void *), stock_touch_trampoline(void *, void *),
    stock_paint_trampoline(void *, void *), stock_dispatch_trampoline(void *, void *),
    stock_keylong_trampoline(void *, void *), stock_paint_bg_trampoline(void *, void *),
    stock_playing_trampoline(void *, void *), stock_display_trampoline(void *, void *),
    stock_localmusic_trampoline(void *, void *), stock_keydown_trampoline(void *, void *),
    stock_sleep_trampoline(void *), stock_color_trampoline(void *, void *, const char *, unsigned),
    stock_image_trampoline(void *, const char *, void *), stock_about_trampoline(void *, void *),
    stock_folder_trampoline(void *, void *), stock_folder_back_trampoline(void *, void *),
    stock_input_trampoline(void *, void *), stock_buzzer_trampoline(int),
    stock_localclass_trampoline(int), stock_power_trampoline(void *, void *),
    stock_audioset_trampoline(void *, void *), stock_confirm_dialog_trampoline(void *, void *),
    stock_playermore_trampoline(void *, void *), stock_playlist_rows_trampoline(void *);
extern void *coverflow_tracks(void *page);
extern void *coverflow_album(void *page), *coverflow_album_tracks(void *r);
extern unsigned coverflow_scope(void *page);
extern void coverflow_home_art(void *top);
extern void coverflow_home_layout(void);
extern void coverflow_home_clip(void *w, void *canvas, int begin);
extern void coverflow_paint(void *w, void *canvas), photos_paint(void *w, void *canvas),
    photos_open(const char *root), books_paint(void *w, void *canvas),
    books_open(const char *root, int videos), video_poll(void), video_key(unsigned key);
extern int books_key(void *top, unsigned key), video_on(void);
extern void visualizer_paint(void *w, void *canvas), visualizer_attach(void *win);
extern void *queue_now(unsigned *pos, unsigned *n);
extern const char *now_tag(void *r, int field);
extern int scrobble_ready(void), scrobble_start(void), scrobble_poll(int *sent);
extern void scrobble_append(const char *line, unsigned n);
extern void *staged(int (*query)(void *), void *arg, int *count);
extern volatile unsigned library_gen;
extern const char *track_name(char *buf, unsigned size, void *t);
#define STOP 11
#define GLIDE_MS 300
#define SCROLL_MARGIN 12
#define DOUBLE_CLICK_MS 200
/* Wake has no delayed single-click action; allow for human timing and the dark UI loop. */
#define WAKE_DOUBLE_CLICK_MS 500
#define NP_SEEK_STEP 5
#define NP_SEEK_MS 250
#define HOME_FAST_WINDOW_MS 200
#define HOME_SLIDE_MS 200
#define HOME_FAST_SLIDE_MS 120
#define WHEEL_RUN_MS 140
/* iPod row lists: the second tick after a stall is overshoot when it comes this long after the
 * first: sooner is a spin, later a tick of its own. Tuned on the device. */
#define OVERSHOOT_MIN_MS 80
#define OVERSHOOT_MAX_MS 140
#define WHEEL_RAMP_MS 100 /* pixel fallback and Stock: one step for this long, then */
#define WHEEL_MAX_STEP 8  /* one more per this much spin, up to this many */
#define LIST_FIRST_MS 300 /* iPod row lists: one row per step for this long, */
#define LIST_RAMP_MS 200  /* then one row more per this much spin */
#define SHORT_LIST_MAX 16
#define MAX_ENTRIES 512
#define POS_MEM 64
/* RADIUS, FILL_RGB, FILL_ALPHA, SHADE_ALPHA, OUTLINE_RGB and OUTLINE_ALPHA come from offsets.inc;
 * the colors pack their bytes at compile time (little-endian r,g,b,a). The outline is translucent
 * neutral white, never the playing red, seated on a dark shade of the stock surface so bright
 * album art cannot wash it out. */
#define FILL_COLOR ((FILL_ALPHA << 24) | FILL_RGB)
#define SHADE_COLOR ((SHADE_ALPHA << 24) | FILL_RGB)
#define OUTLINE_COLOR ((OUTLINE_ALPHA << 24) | OUTLINE_RGB)
#define B(p, o) (*(unsigned char *)((char *)(p) + (o)))

typedef struct {
    int ctx, id; /* logical row + 1; 0 = unused */
    unsigned scope, hash, hash2;
} position_t;

/* Writable state lives in the zero-filled page the builder maps past the payload text.
 * center_timer defers confirmation; wheel_* scale repeated fast detents; pos_* remember
 * the selected row per audited context across page recreation; reveal_* pin the recall glide so
 * an interruption cannot silently rewrite that memory. */
typedef struct {
    unsigned last_center; /* release time of the pending selection press */
    unsigned center_timer, center_token, center_scope, center_hash, center_hash2;
    int center_id, center_ctx, center_rows;
    unsigned last_home;
    int home_dir;
    void *home_surface;   /* non-null also marks a step accepted at time zero */
    void *center_top;     /* top window of that press */
    void *center_surface; /* navigation surface of that press */
    unsigned last_wheel;  /* time of the previous wheel detent */
    int wheel_dir;        /* direction of that detent */
    unsigned wheel_run;   /* continuous same-direction spin ms + 1; capped at full speed */
    int wheel_tick; /* that detent's place in its run: 1 first, 2 overshoot, 3 later; 0 none */
    int touch_mode; /* session-wide drawing preference, independent of selection */
    void *wheel_top, *wheel_surface;
    unsigned wheel_scope;
    int wheel_ctx;
    /* iPod: a Return by button ends touch mode once stock has handled it */
    unsigned untouch_timer;
    position_t pos[POS_MEM]; /* most recently selected first; keyed by context and scope */
    void *reveal_surface;    /* surface of the interrupted recall glide, 0 when none */
    int reveal_id;           /* logical row that glide was bringing into view */
    /* Bump repaint and wrap arm; the widget token rejects recycled surfaces. */
    void *fx_surface;
    int fx_token, bump_dir, edge_dir, edge_id;
    unsigned fx_timer, edge_time;
    /* Resume: the saved places, most recent first; the track polled (rs_key, 0 none) and whether
     * it is long, its last seconds, the seconds last saved and how many polls they stood still;
     * rs_pending is the place to restore once it plays, rs_settle a track change one poll old. */
    struct {
        unsigned key;
        int sec;
    } spots[RESUME_SLOTS];
    unsigned spots_read, rs_at, rs_key;
    int rs_long, rs_settle, rs_sec, rs_total, rs_saved, rs_still, rs_pending;
    int rs_spoken; /* under Podcasts or Audiobooks: resumed at any length, never counted */
    /* Play counts by path hash (plays_read once loaded); the playing track's last second, the
     * seconds of it heard and whether this play has counted. */
    struct {
        unsigned key, n;
    } plays[PLAYS_SLOTS];
    unsigned plays_read, ls_key;
    int ls_sec, ls_heard, ls_done;
    /* Most Played's page and its tracks: the last ranking plus the songs counted since, kept across
     * opens while the library is at mp_gen (~library_gen once stale). */
    void *mp_page, *mp_list;
    unsigned mp_gen;
    int dark; /* the backlight was off at the last UI loop pass */
    /* Power management's Charge limit and Low power (read once from config.ini's Q2POD), the value
     * labels of those rows and Artists, and the poll: charging is held off at the limit; CPU1 is
     * offline (cpu_off), this boot's first offline is done (cpu_marked), and cpu_bad is 0 before
     * the check, 1 usable, 2 refused by an earlier boot's stall, 3 refused by the kernel;
     * last_input times the screen-on idle. */
    int pod_read, charge_limit, low_power, single_wake, charge_held, cpu_off, cpu_bad, cpu_marked;
    void *pod_label[4];
    unsigned charge_at, last_input, cpu_retry;
    unsigned editor_timer, editor_at;
    void *editor_page;
#if IPOD
    unsigned library_page_generation;
    void *pull_page, *pull_surface;
    void *sel_w; /* the surface whose selection was last drawn: its row and centre, for Home's > */
    int sel_row, sel_y;
    int pull_x, pull_y, pull_claimed;
    unsigned pull_scope;
    unsigned clock_key; /* the clock's minute of the day + 1; 0 before the first, ~0 for --:-- */
    /* The status bar's Bluetooth, Wi-Fi and battery widgets, looked up once (the bar is never
     * destroyed); the codec badge shown (index + 1 in BT_CODECS, 0 none), its fade step (0 showing,
     * CODEC_STEPS the glyph fading in, 2 * CODEC_STEPS done) and timer; the battery slot's last
     * level, charge and low state. */
    void *bar_bt, *bar_wifi, *bar_pct, *bar_slot, *bar_icon;
    int codec, codec_step;
    unsigned codec_timer, batt_key;
    unsigned letter_timer; /* the fast-scroll letter shows while this runs */
    /* Now Playing's window and payload-filled widgets, and the sources they last showed. */
    void *np_win, *np_pos, *np_album, *np_slider, *np_remain, *np_elapsed, *np_cover;
    void *np_slide, *np_lrc; /* the art/lyrics/info pages and the lyric lines' scroll_view */
    void *np_control, *np_seek_queue;
    int np_panel, np_mode, np_seek_value, np_seek_pos, np_seek_changed;
    unsigned np_seek_timer, np_seek_hash;
    /* Volume: the dialog vol_paint last drew, its value and the redraw timer */
    void *vol_dialog;
    int vol_drawn;
    unsigned vol_timer;
    unsigned np_hash; /* of the album text, position and queue length shown, 0 to refresh */
    int np_left;
    /* One Centre cycles the bottom control; two sleep. */
    unsigned np_press, np_press_at;
    unsigned lyric_timer; /* runs while the wheel holds the lyrics and stock's timer is stopped */
    /* The Display settings, read from config.ini on first use, and the display page's value labels.
     */
    int settings_read, accent, home_full, battery;
    void *setting_label[3];
    unsigned tone_key; /* the wheel key whose press ringnav_keydown silenced, 0 when none */
    int greeted;       /* the first reachable list got its boot repaint */
#endif
    /* Held centre/Play: the AWTK press time marks its release; the target is a track/list row
     * checked by count, record and browsing-state hashes; qm_cls is the class its list holds;
     * qm_forced is a shuffle Play next. */
    unsigned long long hold_press;
    unsigned unlock_at;
    int unlock_waiting;
    unsigned qm_timer, qm_cls, qm_idx, qm_rows, qm_hash, qm_browse, qm_forced, qm_forced_hash;
    int qm_kind, qm_action;
    void *qm_dialog;
    unsigned char qm_classinfo[912]; /* g_local_classinfo_save before a Go to */
    /* Podcasts/Audiobooks: folder_page opens at media_root once media_open is set; while the page
     * is there, Back at that root leaves it. */
    int media_open;
    char media_root[32];
} scratch_t;
static scratch_t st __attribute__((section(".scratch")));

typedef struct {
    int x, y, w, h;
} rect_t;
typedef struct {
    void *w, *at[MAX_ENTRIES];
    int id[MAX_ENTRIES], n, kind, rows, row, height, ctx;
    unsigned scope;
} menu_t;
/* Single shared view: no entry point keeps a menu live across a nested load(). */
static menu_t g_menu __attribute__((section(".scratch")));

/* Audited top windows, each tagged with its content-identity class and whether its row list is
 * one of the audited local row lists that carry over at the ends. The index is remembered
 * instead of the name pointer: AWTK owns and frees the window's name string. */
enum { CTX_DYNAMIC, CTX_FIXED, CTX_FOLDER, CTX_LOCAL };
enum { RING = 1, DRILL = 2, BUTTONS = 4, CHOICE = 8 };
typedef struct {
    const char *name;
    unsigned char kind, flags;
} context_t;
static const context_t contexts[] = {
#include "contexts.inc"
};

static int context_id(const char *name) {
    if (!name) return -1;
    for (unsigned i = 0; i < sizeof(contexts) / sizeof(*contexts); ++i)
        if (!tk_strcmp(name, contexts[i].name)) return (int)i;
    return -1;
}

#if IPOD
static int selection_window(void *w) {
    int ctx = w ? context_id(widget_get_prop_str(w, "name", "")) : -1;
    return ctx >= 0 && (contexts[ctx].flags & (BUTTONS | CHOICE));
}
#endif

/* The audited local row lists built from the compact assets: the file and music views. Grids
 * (album_page), settings menus, dynamic pages and the home carousel keep hard ends. */
static int ring_list(const menu_t *m) {
    return m->w && m->ctx >= 0 && (contexts[m->ctx].flags & RING) && m->rows >= 2;
}

/* The view kinds load() actually navigates: a vertical scroll view, a table client, a slide
 * menu or a BUTTONS window, which does not scroll. A horizontal or page-snapping scroll
 * view is not a candidate, so it cannot make a page look like it has two panes. */
static int kind(void *w) {
    const char *t = widget_get_type(w);
    if (!tk_strcmp(t, "slide_menu")) return 3;
    if (!tk_strcmp(t, "table_client")) return 2;
    if (!tk_strcmp(t, "dialog") || !tk_strcmp(t, "window")) {
        int ctx = context_id(widget_get_prop_str(w, "name", (void *)0));
        return ctx >= 0 && (contexts[ctx].flags & BUTTONS) ? 4 : 0;
    }
    return !tk_strcmp(t, "scroll_view") && B(w, VIEW_VERTICAL) && !B(w, VIEW_HORIZONTAL) &&
                   !B(w, VIEW_SNAP)
               ? 1
               : 0;
}

/* Collect up to two candidate panes. ponytail: bounded tree walk per event; a persistent cache
 * would go stale because the tree changes without notification, so it waits for a measured
 * stutter. */
static void find_surface(void *w, void **found, int *count, int *aborted, int depth, int *budget) {
    if (!w || !widget_get_visible(w) || !widget_get_prop_bool(w, "enable", 1)) return;
    if (depth == 16 || --*budget < 0) {
        *aborted = 1;
        return;
    }
    if (kind(w)) {
        if (*count < 2) found[*count] = w;
        ++*count;
        return;
    }
    if (!tk_strcmp(widget_get_type(w), "pages")) {
        int active = widget_get_prop_int(w, "active", -1);
        if (active >= 0)
            find_surface(widget_get_child(w, active), found, count, aborted, depth + 1, budget);
        return;
    }
    unsigned n = widget_count_children(w);
    for (unsigned i = 0; i < n && *count < 2; ++i)
        find_surface(widget_get_child(w, i), found, count, aborted, depth + 1, budget);
}

static int clamp_step(int offset, int maximum, int delta) {
    if (maximum < 0) maximum = 0;
    offset = offset < 0 ? 0 : offset > maximum ? maximum : offset;
    if (delta > maximum - offset) return maximum;
    if (delta < -offset) return 0;
    return offset + delta;
}

/* One-row steps until a sustained run of accepted same-direction ticks has spun for `first` ms;
 * the step is then two and gains one per `more` ms, capped at WHEEL_MAX_STEP. A pause longer than
 * WHEEL_RUN_MS, a reversal or a change of menu resets the run, so stopping and reversing stay
 * precise. wheel_tick places the tick in its run for iPod's overshoot filter: the first after a
 * stall, a reversal or a change of menu, a second one OVERSHOOT_MIN_MS to OVERSHOOT_MAX_MS behind
 * it, or any other. */
static int ramp(void *top, void *surface, unsigned scope, int ctx, int dir, unsigned now, int first,
                int more) {
    int same = st.wheel_dir == dir && st.wheel_top == top && st.wheel_surface == surface &&
               st.wheel_scope == scope && st.wheel_ctx == ctx;
    unsigned gap = now - st.last_wheel;
    int run = same && st.wheel_tick && gap <= OVERSHOOT_MAX_MS;
    st.wheel_tick = !run ? 1 : st.wheel_tick == 1 && gap >= OVERSHOOT_MIN_MS ? 2 : 3;
    if (same && st.wheel_run && gap <= WHEEL_RUN_MS)
        st.wheel_run =
            (unsigned)clamp_step(st.wheel_run, first + (WHEEL_MAX_STEP - 2) * more + 1, gap);
    else
        st.wheel_run = 1;
    st.last_wheel = now;
    st.wheel_dir = dir;
    st.wheel_top = top;
    st.wheel_surface = surface;
    st.wheel_scope = scope;
    st.wheel_ctx = ctx;
    int spin = (int)st.wheel_run - 1;
    return spin < first ? 1 : 2 + (spin - first) / more;
}

/* A tap target has an EVT_CLICK handler. V1.32 widget emitter @0x60; emitter_on_with_tag items are
 * {ctx, id, type @8, handler, tag, working @0x14, pending_remove @0x15, next @0x20}. */
static int clickable(void *w) {
    void *emitter = P(w, W_EMITTER);
    for (void *item = emitter ? P(emitter, 0) : (void *)0; item; item = P(item, EMIT_NEXT))
        if (I(item, EMIT_TYPE) == EVT_CLICK && !B(item, EMIT_PENDING_REMOVE)) return 1;
    return 0;
}

typedef struct {
    void **at;
    int n, cap, budget;
} entries_t;

/* Visible, enabled tap targets in pre-order; a target's descendants belong to it.
 * ponytail: capped walk; raise MAX_ENTRIES if a real list outgrows 512. */
static void collect(void *w, entries_t *s, int depth) {
    if (!w || !widget_get_visible(w) || !widget_get_prop_bool(w, "enable", 1)) return;
    if (depth == 16 || s->n == s->cap || --s->budget < 0) return;
    if (clickable(w)) {
        s->at[s->n++] = w;
        return;
    }
    /* A pages widget exposes only its active child; inactive tabs must not look tappable. */
    if (!tk_strcmp(widget_get_type(w), "pages")) {
        int active = widget_get_prop_int(w, "active", -1);
        if (active >= 0) collect(widget_get_child(w, active), s, depth + 1);
        return;
    }
    unsigned n = widget_count_children(w);
    for (unsigned i = 0; i < n; ++i) collect(widget_get_child(w, i), s, depth + 1);
}

/* Widget-owned properties die with the surface; never retain recycled row pointers. */
#define SEL "_ringnav_index"
#define TOUCH "_ringnav_touch"
#define COUNT "_ringnav_count"
#define SCOPE "_ringnav_scope"
#define FX "_ringnav_fx"

/* UI timers, shared with coverflow.c; a failed add leaves the timer 0. */
void stop_timer(unsigned *timer) {
    if (*timer) timer_remove(*timer);
    *timer = 0;
}

void rearm(unsigned *timer, int (*fn)(const void *), unsigned ms) {
    stop_timer(timer);
    *timer = timer_add(fn, (void *)0, ms);
}

static void cancel_center(void) {
    stop_timer(&st.center_timer);
    st.center_top = st.center_surface = (void *)0;
}

/* The list's spin ramp starts over, and the next tick is the first of its run. */
static void drop_wheel(void) {
    st.wheel_run = 0;
    st.wheel_tick = 0;
}

/* A wheel or centre step supersedes any run: drop the spin ramp and home slide. */
static void drop_spin(void) {
    drop_wheel();
    st.home_surface = (void *)0;
}

/* Input that is not navigation here: no pending confirmation and no run survive it. */
static void drop_input(void) {
    cancel_center();
    stop_timer(&st.editor_timer);
    st.editor_page = (void *)0;
    drop_spin();
    st.unlock_waiting = 0;
}

/* Home and Coverflow step their slide_menu with one retargeted animator (home_step): stock
 * scroll_to (0x5f3400) starts a new animator per call and orphans the running one, so fast
 * ticks leave several fighting over the offset and committing the index twice. */
static int carousel_page(void *top) {
    const char *name = top ? widget_get_prop_str(top, "name", "") : "";
    return !tk_strcmp(name, "home_page") || !tk_strcmp(name, "coverflow_page") ||
           !tk_strcmp(name, "photos_page"); /* its viewer */
}

static int is_home(void *top, void *w) { return w && kind(w) == 3 && carousel_page(top); }

#if IPOD
/* A widget's own window: the top-level widget it sits in. */
static void *window_of(void *w) {
    void *wm = window_manager();
    while (w && P(w, W_PARENT) != wm) w = P(w, W_PARENT);
    return w;
}

/* A page that opens and closes with stock's slide (tools/ipod.py SLIDE). Stock paints it and
 * the page under it into the animator's snapshots while it is the top window and before
 * window_manager_is_animating is set; nothing else paints the page under it. */
static int slides(void *win) {
    const char *hint = win ? widget_get_prop_str(win, "anim_hint", (void *)0) : (void *)0;
    return hint && *hint;
}
#else
#define slides(win) 0
#endif

/* The animator's destination is the intended icon, even before stock commits its index.
 * Keep this widget-owned: touch and page recreation cannot leave a dangling animator here. */
static int slide_index(void *w) {
    void *a = P(w, SLIDE_ANIMATOR);
    int n = (int)widget_count_children(w);
    if (!n) return -1;
    int id = I(w, SLIDE_INDEX);
    int stride = slide_menu_item_width(w) + I(w, SLIDE_SPACER);
    if (a && stride > 0) id -= I(a, ANIM_X_TO) / stride;
    id %= n;
    return id < 0 ? id + n : id;
}

static int home_done(void *w, void *event) {
    slide_menu_on_scroll_done(w, event);
    /* Stock skips focus restoration when a reversal returns to the original index. */
    widget_set_focused(widget_get_child(w, I(w, SLIDE_INDEX)), 1);
    return 7; /* RET_REMOVE: same one-shot lifetime as stock completion */
}

static void home_step(void *w, int dir, unsigned now) {
    int n = (int)widget_count_children(w);
    int stride = slide_menu_item_width(w) + I(w, SLIDE_SPACER);
    int fast =
        st.home_surface == w && st.home_dir == dir && now - st.last_home <= HOME_FAST_WINDOW_MS;
    void *a = P(w, SLIDE_ANIMATOR);
    int live = I(w, SLIDE_OFFSET);
    int goal = a ? I(a, ANIM_X_TO) : 0;
    /* Reversal discards the unfinished destination and heads to the adjacent card
     * behind the live position. Same-direction ticks extend the intended destination. */
    if (a && (goal - live) * dir > 0) {
        goal = (live / stride) * stride;
        if (dir > 0 ? goal >= live : goal <= live) goal -= dir * stride;
    } else
        goal -= dir * stride;
    if (!a) {
        a = widget_animator_scroll_create(w, HOME_SLIDE_MS, 0, SLIDE_EASING);
        if (a && !widget_animator_on(a, EVT_ANIM_END, home_done, w)) {
            widget_animator_destroy(a);
            a = (void *)0;
        }
        P(w, SLIDE_ANIMATOR) = a;
        if (!a) {
            int id = (I(w, SLIDE_INDEX) - goal / stride) % n;
            I(w, SLIDE_OFFSET) = 0;
            slide_menu_set_value(w, id < 0 ? id + n : id);
            widget_set_focused(widget_get_child(w, I(w, SLIDE_INDEX)), 1);
            st.home_surface = (void *)0;
            return;
        }
        widget_set_focused(widget_get_child(w, I(w, SLIDE_INDEX)), 0);
    }
    widget_animator_pause(a);
    widget_animator_scroll_set_params(a, live, 0, goal, 0);
    I(a, ANIM_ELAPSED) = I(a, ANIM_START_TIME) = 0;
    I(a, ANIM_DURATION) = fast ? HOME_FAST_SLIDE_MS : HOME_SLIDE_MS;
    widget_animator_start(a);
    st.home_surface = w;
    st.last_home = now;
    st.home_dir = dir;
}

static int usable(void) {
    if (!g_backlight_status || g_lockscreen_pageflag || g_testmode_flag || g_guideflag ||
        g_poweroff_state || bt__recv_pageflag) return 0;
    if (g_usblink_status != 2) return 1;
#if IPOD
    /* usbmode_exit waits inside this modal prompt before clearing USB mode. The cable is
     * already out, but the stale mode must not disable the prompt's wheel and Centre. */
    if (!g_usbdet_value) {
        void *top = window_manager_get_top_window(window_manager());
        return top && !tk_strcmp(widget_get_prop_str(top, "name", ""), "confirminfo_dialog") &&
               widget_get_prop_int(top, "_scan_after_usb", 0);
    }
#endif
    return 0;
}

static int allowed_top(void *top) {
    return top && context_id(widget_get_prop_str(top, "name", (void *)0)) >= 0;
}

/* Prefer a populated row pane over an empty placeholder. Text-only panes retain pixel scrolling. */
static int pane_rows(void *w) {
    if (kind(w) == 2) return I(w, TABLE_ROWS) > 0;
    void *row = (void *)0;
    entries_t s = { &row, 0, 1, 4096 };
    unsigned n = widget_count_children(w);
    for (unsigned i = 0; i < n && !s.n; ++i) collect(widget_get_child(w, i), &s, 1);
    return s.n;
}

/* A click identifies its pane by ancestry. An unowned pair starts with the first populated pane
 * in UI order; painting and touch never choose one implicitly. */
static void *surface_under(void *top, void *target, void **other, int wheel) {
    void *found[2] = { (void *)0, (void *)0 };
    int count = 0, aborted = 0, budget = 512;
    find_surface(top, found, &count, &aborted, 0, &budget);
    if (aborted || count == 0) return (void *)0;
    if (count == 1) return found[0];
    if (target) {
        for (int depth = 0; target && depth < 32; ++depth) {
            for (int i = 0; i < count; ++i)
                if (target == found[i]) {
                    if (other) *other = found[1 - i];
                    return found[i];
                }
            if (target == top) break;
            target = P(target, W_PARENT);
        }
        return (void *)0;
    }
    int first = widget_get_prop_int(found[0], SEL, -1) >= 0;
    int second = widget_get_prop_int(found[1], SEL, -1) >= 0;
    if (wheel && !(first && second)) {
        int rows[2] = { pane_rows(found[0]), pane_rows(found[1]) };
        if (!first && !second) return !rows[0] && rows[1] ? found[1] : found[0];
        int owner = second ? 1 : 0;
        if (!rows[owner] && rows[1 - owner]) {
            widget_set_prop_int(found[owner], SEL, -1);
            widget_invalidate_force(found[owner], (void *)0);
            return found[1 - owner];
        }
    }
    return first == second ? (void *)0 : (first ? found[0] : found[1]);
}

static void *surface(void *target, void **other) {
    void *wm = window_manager(), *top = window_manager_get_top_window(wm);
    void *w = usable() && !window_manager_is_animating(wm) && allowed_top(top)
                  ? surface_under(top, target, other, 0)
                  : (void *)0;
    if (st.center_timer && (top != st.center_top || w != st.center_surface)) cancel_center();
    if (!is_home(top, w) || w != st.home_surface) st.home_surface = (void *)0;
    return w;
}

static void prop(void *w, const char *name, int value) {
    if (widget_get_prop_int(w, name, -1) != value) widget_set_prop_int(w, name, value);
}

/* A payload page's own choice of row (photos.c: the photo last viewed), kept as a step would. */
void ringnav_select(void *w, int id, int rows) {
    prop(w, COUNT, rows);
    prop(w, SEL, id);
}

/* The bump timer repaints at 120 ms; the second-detent arm survives independently. */
static void fx_cancel(void) {
    stop_timer(&st.fx_timer);
    st.fx_surface = (void *)0;
    st.bump_dir = 0;
}

static int fx_live(void *w) {
    return w == st.fx_surface && widget_get_prop_int(w, FX, 0) == st.fx_token;
}

static int fx_expire(const void *info) {
    (void)info;
    st.fx_timer = 0;
    void *w = surface((void *)0, (void *)0);
    st.bump_dir = 0;
    if (w && fx_live(w)) widget_invalidate_force(w, (void *)0);
    return 0;
}

static void fx_arm(void *w, int dir) {
    rearm(&st.fx_timer, fx_expire, BUMP_MS);
    st.fx_token = st.fx_token == 0x7fffffff ? 1 : st.fx_token + 1;
    st.fx_surface = w;
    st.bump_dir = st.fx_timer ? dir : 0;
    prop(w, FX, st.fx_token);
}

/* A boundary detent arms only while the selection stays on that row; leaving it or reversing
 * makes the next boundary detent bump again. Every stopped detent re-arms, so a continuing spin
 * hard-stops at the end; only a detent after a pause of EDGE_PAUSE_MS carries over. */
static int edge_wraps(const menu_t *m, int id, int dir, unsigned now) {
    return id >= 0 && fx_live(m->w) && st.edge_id == id && st.edge_dir == dir &&
           now - st.edge_time >= EDGE_PAUSE_MS;
}

/* FNV-1a of the first two non-empty text properties in a small row subtree: the item's own name
 * and an optional subtitle, in pre-order. Each stays 0 while its text has not been seen. No stock
 * list row carries a stable id: emitter tags and pointer props are unused by the app rows (only
 * ROW_INDEX, which a re-sort rewrites), so these hashes are the identity available to restore. */
typedef struct {
    unsigned one, two;
    const unsigned *title; /* the first text itself, for the iPod fast-scroll letter */
} row_id_t;

/* The library browsing state both position memory and the queue menu key on. */
static unsigned local_hash(unsigned h) {
    h = hash_bytes(h, g_class_type, 4);
    h = hash_bytes(h, g_local_classinfo_save, 912);
    return hash_bytes(h, g_artist_type, 4);
}

static void row_hash_text(const unsigned *s, unsigned *h) {
    unsigned n = 0;
    while (s[n]) ++n;
    /* Stock wchar_t is UTF-32; hash all four bytes, zero bytes inside a character included. */
    *h = hash_bytes(FNV_SEED, (const unsigned char *)s, 4 * n);
    if (!*h) *h = 1; /* zero denotes missing text */
}

static void row_id_walk(void *w, int depth, int *budget, row_id_t *id) {
    if (!w || depth == 4 || id->two || --*budget < 0) return;
    const unsigned *s = widget_get_text(w);
    if (s && *s) {
        if (!id->one) {
            row_hash_text(s, &id->one);
            id->title = s;
        } else {
            row_hash_text(s, &id->two);
            return;
        }
    }
    unsigned n = widget_count_children(w);
    for (unsigned i = 0; i < n; ++i) row_id_walk(widget_get_child(w, i), depth + 1, budget, id);
}

static row_id_t row_id(void *w) {
    row_id_t id = { 0, 0, (void *)0 };
    int budget = 32;
    row_id_walk(w, 0, &budget, &id);
    return id;
}

/* The local list loaders use these browsing globals: folder_enter/back maintain g_folder_path;
 * load_localclass_list/load_album_detaillist use the class, saved query and artist/album modes.
 * Hash the bounded query object, including its flags, rather than a title or a freed pointer. */
static int context_now(unsigned *scope) {
    void *top = window_manager_get_top_window(window_manager());
    const char *name = top ? widget_get_prop_str(top, "name", (void *)0) : (void *)0;
    *scope = 0;
    if (!name) return -1;
    int ctx = context_id(name);
    if (ctx < 0) return -1;
    void *found[2];
    int count = 0, aborted = 0, budget = 512;
    find_surface(top, found, &count, &aborted, 0, &budget);
    if (contexts[ctx].kind == CTX_FOLDER) {
        unsigned n = 0;
        while (n < 1024 && g_folder_path[n]) ++n;
        if (!n || n == 1024) return -1;
        *scope = hash_bytes(FNV_SEED, g_folder_path, n);
    } else if (contexts[ctx].kind == CTX_LOCAL) {
        *scope = hash_bytes(local_hash(FNV_SEED), album_modetype, 4);
    } else if (contexts[ctx].kind == CTX_FIXED) {
        *scope = 1;
    } else if (!tk_strcmp(name, "coverflow_page")) {
        *scope = coverflow_scope(top);
        if (!*scope) return -1;
    } else {
        if (!tk_strcmp(name, "search_dialog") || !tk_strcmp(name, "tidal_search_dialog")) {
            void *edit = widget_lookup(top, "search_edit", 1);
            row_id_t query = row_id(edit);
            *scope = query.one;
        }
        /* Network/detail pages without an audited content key keep widget-owned selection, but
         * never import another page's row. */
        return -1;
    }
    if (!*scope) *scope = 1;
    return !aborted && count == 1 ? ctx : -1; /* no cross-pane position memory */
}

static int index_of(menu_t *m, int id);
static rect_t bounds(menu_t *m, int i);
static void reveal(menu_t *m, int id, int immediate);

/* Bounded recency order avoids a timestamp that could wrap during a long session. */
static int position(menu_t *m) {
    if (m->ctx >= 0)
        for (int i = 0; i < POS_MEM; ++i)
            if (st.pos[i].id && st.pos[i].ctx == m->ctx && st.pos[i].scope == m->scope) return i;
    return -1;
}

/* Remember where the user was. Widget props die with a recreated page; this survives it.
 * Non-virtual lists also remember the row's first two text values, so a reordered list restores
 * the same item and duplicate names can be told apart by their second line. */
static void select(menu_t *m, int id) {
    if (st.center_timer && (m->w != st.center_surface || id != st.center_id)) cancel_center();
    if (st.reveal_surface == m->w) st.reveal_surface = (void *)0;
    unsigned hash = 0, hash2 = 0;
    if (id >= 0) {
        int ctx = m->ctx;
        if (m->kind == 1) {
            int i = index_of(m, id);
            if (i >= 0) {
                row_id_t r = row_id(m->at[i]);
                hash = r.one;
                hash2 = r.two;
            }
        }
        if (ctx >= 0) {
            int p = position(m);
            if (p < 0) p = POS_MEM - 1;
            for (; p > 0; --p) st.pos[p] = st.pos[p - 1];
            st.pos[0] = (position_t){ ctx, id + 1, m->scope, hash, hash2 };
        }
    }
    prop(m->w, SEL, id);
    if (m->kind == 2 && m->rows > I(m->w, TABLE_ROWS)) {
        int footer = index_of(m, I(m->w, TABLE_ROWS));
        if (footer >= 0) widget_invalidate_force(m->at[footer], (void *)0);
    }
}

static int control_available(void *w, void *top) {
    for (int depth = 0; w && depth < 32; ++depth, w = P(w, W_PARENT)) {
        if (!widget_get_visible(w) || !widget_get_prop_bool(w, "enable", 1)) return 0;
        if (w == top) return 1;
    }
    return 0;
}

static int load_rows(menu_t *m, void *w) {
    m->w = w;
    m->n = 0;
    m->kind = kind(w);
    m->height = I(w, W_H);
    if (!m->kind || m->height <= 0) return 0;
    m->row = m->kind == 2 ? I(w, ROW_HEIGHT) : 0;
    m->rows = m->kind == 2 ? I(w, TABLE_ROWS) : 0;
    if (m->kind == 2 && (m->row <= 0 || m->rows < 0 || m->rows > 0x7fffffff / m->row)) return 0;
    unsigned n = widget_count_children(w);
    if (m->kind == 1 && I(w, VIEW_CONTENT_H) < 0) return 0;
    if (m->kind == 1 || m->kind == 4) {
        entries_t s = { m->at, 0, MAX_ENTRIES, 4096 };
        for (unsigned i = 0; i < n; ++i) {
            void *child = widget_get_child(w, i);
            if (m->kind == 4 && !tk_strcmp(widget_get_prop_str(child, "name", ""), "view_navbar")) continue;
            collect(child, &s, 1);
        }
        m->n = s.n;
        m->rows = s.n;
        for (int i = 0; i < m->n; ++i) m->id[i] = i;
    } else {
        if (m->kind == 3) m->rows = (int)n;
        for (unsigned i = 0; i < n && m->n < MAX_ENTRIES; ++i) {
            void *r = widget_get_child(w, i), *e = (void *)0;
            entries_t s = { &e, 0, 1, 256 };
            collect(r, &s, 0);
            int id = m->kind == 2 ? I(r, ROW_INDEX) : (int)i;
            if (e && id >= 0 && id < m->rows) {
                m->at[m->n] = e;
                m->id[m->n++] = id;
            }
        }
    }
    /* Folder scanning has a native footer outside the virtual table. Give it the final logical
     * index without changing the table's physical row count or recycled pool. */
    void *top = window_manager_get_top_window(window_manager());
    if (m->kind == 2 && top &&
        !tk_strcmp(widget_get_prop_str(top, "name", ""), "specfolder_page")) {
        void *footer = widget_lookup(top, "btn_startscan", 1);
        if (control_available(footer, top) &&
            clickable(footer) && m->n < MAX_ENTRIES && m->rows < 0x7fffffff) {
            m->at[m->n] = footer;
            m->id[m->n++] = m->rows++;
        }
    }
    return 1;
}

static int footer_row(const menu_t *m, int id) {
    return m->kind == 2 && id == I(m->w, TABLE_ROWS) && m->rows > id;
}

#if IPOD
/* Native option builders mark the current choice with the select image. */
static int checked_choice(void *w, int depth, int *budget) {
    if (!w || depth == 16 || --*budget < 0 || !widget_get_visible(w)) return 0;
    if (!tk_strcmp(widget_get_prop_str(w, "image", ""), "select")) return 1;
    unsigned n = widget_count_children(w);
    for (unsigned i = 0; i < n; ++i)
        if (checked_choice(widget_get_child(w, i), depth + 1, budget)) return 1;
    return 0;
}

static int selection_default(const menu_t *m) {
    if (!selection_window(window_manager_get_top_window(window_manager()))) return -1;
    int requested = widget_get_prop_int(m->w, "_selection_default", -1);
    if (requested >= 0 && requested < m->rows) return requested;
    for (int i = 0; i < m->n; ++i) {
        int budget = 256;
        if (checked_choice(m->at[i], 0, &budget)) return m->id[i];
    }
    return m->rows ? 0 : -1;
}
#endif

static void reveal(menu_t *m, int id, int immediate);

static int load(menu_t *m, void *w, int recall) {
    if (!load_rows(m, w)) return 0;
    m->ctx = context_now(&m->scope);
    int count = widget_get_prop_int(w, COUNT, -1);
    unsigned scope = (unsigned)widget_get_prop_int(w, SCOPE, 0);
    if (scope != m->scope) {
        count = -1;
        prop(w, SCOPE, (int)m->scope);
        if (st.reveal_surface == w) st.reveal_surface = (void *)0;
    }
    if (st.fx_surface && (st.fx_surface != w || scope != m->scope || count != m->rows)) fx_cancel();
    if (count != m->rows) {
        if (st.wheel_surface == w) drop_wheel();
#if IPOD
        prop(w, SEL, selection_default(m));
#else
        prop(w, SEL, -1);
#endif
        prop(w, COUNT, m->rows);
#if IPOD
        int initial = widget_get_prop_int(w, SEL, -1);
        int slot = index_of(m, initial);
        if (slot >= 0 && m->kind == 1) {
            rect_t r = bounds(m, slot);
            if (r.y < 0 || r.y + r.h > m->height) reveal(m, initial, 1);
        }
#endif
    }
    /* A recreated page has never seen this surface (COUNT unset) and lost its selection: put the
     * user back where they left off. A live page whose count changed resets the selection but
     * keeps its viewport, so it does not re-read the table. A non-virtual list prefers the
     * remembered first text, breaks ties by the second text and then by the remembered index,
     * and falls back to the index when no text matches. */
#if IPOD
    if (selection_window(window_manager_get_top_window(window_manager()))) recall = 0;
#endif
    if (recall && m->kind != 3 && count < 0) {
        int p = position(m);
        int id = p < 0 ? -1 : st.pos[p].id - 1;
        unsigned hash = p < 0 ? 0 : st.pos[p].hash;
        unsigned hash2 = p < 0 ? 0 : st.pos[p].hash2;
        if (id >= 0 && m->kind == 1 && hash) {
            /* A secondary-text match outranks proximity; equal ranks keep the earlier row. */
            int best = -1, best_dist = 0, best_second = 0;
            for (int i = 0; i < m->n; ++i) {
                row_id_t r = row_id(m->at[i]);
                if (r.one != hash) continue;
                int dist = m->id[i] - id;
                if (dist < 0) dist = -dist;
                int second = hash2 && r.two == hash2;
                if (best < 0 || second > best_second ||
                    (second == best_second && dist < best_dist)) {
                    best = i;
                    best_dist = dist;
                    best_second = second;
                }
            }
            if (best >= 0) id = m->id[best];
        }
        if (id >= 0 && id < m->rows) {
            prop(w, SEL, id);
            /* A sliding page's first paint is its snapshot: arrive in place, not mid-glide. */
            reveal(m, id, slides(window_manager_get_top_window(window_manager())));
            st.reveal_surface = m->w;
            st.reveal_id = id;
            /* A synchronous table rebind changes the row pool; discard the pre-scroll snapshot. */
            if (m->kind == 2) return load_rows(m, w);
        }
    }
    return 1;
}

/* Live viewport offset. reveal glides are relative, so every delta is computed against the
 * position the widget is actually at, never an intended one. */
static int view_top(menu_t *m) {
    return m->kind == 2 ? I(m->w, TABLE_TOP) : m->kind == 1 ? I(m->w, SCROLL_Y) : 0;
}

/* Largest viewport top that still shows content; the clamp bound for every glide. */
static int max_top(menu_t *m) {
    if (m->kind == 2) return I(m->w, TABLE_ROWS) * m->row - m->height;
    return m->kind == 1 ? I(m->w, VIEW_CONTENT_H) - m->height : 0;
}

static rect_t bounds(menu_t *m, int i) {
    void *e = m->at[i];
    rect_t r = { 0, 0, I(e, W_W), I(e, W_H) };
    for (void *p = e; p && p != m->w; p = P(p, W_PARENT)) {
        r.x += I(p, W_X);
        r.y += I(p, W_Y);
    }
    if (m->kind != 3) r.y -= view_top(m);
    if (m->kind == 1) r.x -= I(m->w, SCROLL_X);
    return r;
}

static int index_of(menu_t *m, int id) {
    for (int i = 0; i < m->n; ++i)
        if (m->id[i] == id) return i;
    return -1;
}

static int moving(menu_t *m) {
    return m->kind == 1   ? P(m->w, VIEW_ANIMATOR) != 0
           : m->kind == 2 ? P(m->w, TABLE_ANIMATOR) != 0
                          : 0;
}

static void stop_scroll(menu_t *m) {
    if (m->kind == 2)
        table_client_stop_animator_scroll(m->w);
    else if (m->kind == 1 && P(m->w, VIEW_ANIMATOR)) {
        /* Same pause/destroy/null sequence as stock table_client_stop_animator_scroll. */
        void *a = P(m->w, VIEW_ANIMATOR);
        widget_animator_pause(a);
        widget_animator_destroy(a);
        P(m->w, VIEW_ANIMATOR) = (void *)0;
    }
}

/* Wheel offsets are synchronous; table setters can replace the recycled row pool. Scroll end
 * loads the covers in view, as after a touch scroll. */
static void wheel_offset(menu_t *m, int top) {
    stop_scroll(m);
    if (m->kind == 2) {
        table_client_set_yoffset(m->w, top);
        load_rows(m, m->w);
    } else if (m->kind == 1)
        scroll_view_set_offset(m->w, I(m->w, SCROLL_X), top);
    else
        return;
    widget_dispatch_simple_event(m->w, EVT_SCROLL_END);
}

/* Least viewport move that reveals logical row id with a small reading margin.
 * Remembered-position restoration keeps its glide; wheel steps are immediate. */
static void reveal(menu_t *m, int id, int immediate) {
    if (footer_row(m, id)) {
        stop_scroll(m);
        return; /* the fixed footer is already visible */
    }
    int top = view_top(m);
    int y, h;
    if (m->kind == 2) {
        y = id * m->row;
        h = m->row;
    } else {
        int i = index_of(m, id);
        if (i < 0) return;
        rect_t r = bounds(m, i);
        y = r.y + top;
        h = r.h;
    }
    /* A tall row cannot fit: show its title consistently instead of alternating edges. */
    int margin = clamp_step((m->height - h) / 2, SCROLL_MARGIN, 0);
    int want = h > m->height || y - top < margin  ? y - margin
               : y - top > m->height - h - margin ? y - (m->height - h - margin)
                                                  : top;
    want = clamp_step(want, max_top(m), 0);
    if (immediate) {
        wheel_offset(m, want);
        return;
    }
    if (want == top) return;
    /* Stock scroll views cannot retarget an animator whose old goal is a boundary. */
    stop_scroll(m);
    if (m->kind == 2) {
        table_client_scroll_to(m->w, want);
    } else
        scroll_view_scroll_delta_to(m->w, 0, want - top, GLIDE_MS);
}

/* Keep selection during native momentum and recall glides. Once settled, repair an offscreen
 * selection with the visible row nearest the viewport centre (ties go to the earlier row), so a
 * swipe never leaves the highlight pinned to the top edge. An interrupted recall glide is
 * retried once instead: the remembered row must not be silently replaced by a visible one. */
static int reconcile(menu_t *m, int settle) {
    int id = m->kind == 3 ? slide_index(m->w) : widget_get_prop_int(m->w, SEL, -1);
    int cur = index_of(m, id);
    if (m->kind == 3) return cur;
    if (id < 0 && m->rows == 1 && footer_row(m, 0)) {
        select(m, 0);
        return index_of(m, 0);
    }
    if (cur >= 0 && footer_row(m, id)) return cur;
    if (cur >= 0) {
        rect_t r = bounds(m, cur);
        if (r.y < m->height && r.y + r.h > 0) {
            st.reveal_surface = (void *)0; /* the glide arrived, or the user brought it back */
            return cur;
        }
        if (!settle) return cur;
    } else if (id >= 0 && id < m->rows && !settle)
        return -1;
    if (st.reveal_surface == m->w && st.reveal_id == id) {
        st.reveal_surface = (void *)0;
        reveal(m, id, 0);
        if (m->kind == 2) return load_rows(m, m->w) ? index_of(m, id) : -1;
        return cur;
    }
    int best = -1, best_dist = 0, partial = -1, partial_dist = 0, had = id >= 0;
    for (int i = 0; i < m->n; ++i) {
        rect_t r = bounds(m, i);
        if (r.y >= m->height || r.y + r.h <= 0) continue;
        int dist = r.y + r.h / 2 - m->height / 2;
        if (dist < 0) dist = -dist;
        if (r.y >= 0 && r.y + r.h <= m->height) {
            if (best < 0 || (had && dist < best_dist)) {
                best = i;
                best_dist = dist;
            }
            if (!had) break; /* a fresh page starts on the first visible row */
        } else if (partial < 0 || (had && dist < partial_dist)) {
            partial = i;
            partial_dist = dist;
        }
    }
    cur = best >= 0 ? best : partial;
    st.reveal_surface = (void *)0;
    select(m, cur >= 0 ? m->id[cur] : -1);
    return cur;
}

/* Resolve only live rows. Widget-owned tokens reject a recreated window/surface even when
 * the allocator reuses its address; text hashes reject a rebound item at the same index. */
#define CONFIRM "_ringnav_confirm"
static int pending_matches(void *top, menu_t *m) {
    int id = m->kind == 3 ? slide_index(m->w) : widget_get_prop_int(m->w, SEL, -1);
    int i = index_of(m, id);
    if (top != st.center_top || m->w != st.center_surface || m->scope != st.center_scope ||
        m->ctx != st.center_ctx || m->rows != st.center_rows || id != st.center_id || i < 0 ||
        (unsigned)widget_get_prop_int(top, CONFIRM, 0) != st.center_token ||
        (unsigned)widget_get_prop_int(m->w, CONFIRM, 0) != st.center_token)
        return 0;
    /* Ordinary rows must still be the armed widget, even with duplicate/missing text.
     * Virtual tables instead resolve the logical row across recycled pool widgets. */
    if (m->kind != 2 && (unsigned)widget_get_prop_int(m->at[i], CONFIRM, 0) != st.center_token)
        return 0;
    row_id_t r = row_id(m->at[i]);
    return r.one == st.center_hash && r.two == st.center_hash2;
}

static int confirm_center(const void *info) {
    (void)info;
    void *w = surface((void *)0, (void *)0);
    if (!st.center_timer) return 0;
    /* Stock removes this one-shot after return; never repeat (RET_REPEAT=8). */
    st.center_timer = 0;
    void *top = window_manager_get_top_window(window_manager());
    int valid = w && !window_manager_get_pointer_pressed(window_manager()) && !g_power_longkey &&
                !g_ingore_bootkey_flag && !*(volatile unsigned char *)BOOT_KEY_GUARD &&
                load_rows(&g_menu, w);
    if (valid) {
        /* Validation must not recall/rebind a changed scope or consume its pending recall. */
        g_menu.ctx = context_now(&g_menu.scope);
        valid = pending_matches(top, &g_menu);
    }
    cancel_center(); /* Clear before any app callback can destroy or navigate the page. */
    if (valid) {
        /* Stock's wheel lockout after the centre key (up to 800 ms) has guarded the press; the
         * page it opens scrolls at once. */
        *(volatile unsigned char *)KEY_LOCKOUT = 0;
        void *target = g_menu.at[index_of(&g_menu, st.center_id)];
        char click[0x30];
        stock_dispatch_trampoline(target, pointer_event_init(click, EVT_CLICK, target, 0, 0));
    }
    return 0;
}

/* Native scroll_to with the bar's current value only wakes its opacity lifecycle.
 * Stock setters retain ownership of thumb position. A transparent bar is still a bar.
 * The native same-value path shows immediately, waits 300 ms and fades over 500 ms. */
static void native_scrollbar(menu_t *m) {
    if (m->kind == 3 || max_top(m) <= 0) return;
    void *parent = P(m->w, W_PARENT);
    if (!parent) return;
    const char *type = widget_get_type(parent);
    if (tk_strcmp(type, "list_view") && tk_strcmp(type, "table_view")) return;
    unsigned n = widget_count_children(parent);
    for (unsigned i = 0; i < n; ++i) {
        void *bar = widget_get_child(parent, i);
        if (!tk_strcmp(widget_get_type(bar), "scroll_bar_m")) {
            scroll_bar_scroll_to(bar, I(bar, BAR_VALUE), 500);
            return;
        }
    }
}

#if IPOD
#define PULL_BOUND "_pull_bound"
#define PULL_SUPPRESS "_pull_suppress"
#define PULL_PROMPT "_pull_prompt"

static void pull_cancel(void) {
    void *page = st.pull_page;
    st.pull_page = st.pull_surface = (void *)0;
    st.pull_claimed = 0;
    /* Stock widget_set_visible rejects a NULL widget; an absent prompt needs no local guard. */
    if (page) widget_set_visible(widget_lookup(page, PULL_PROMPT, 1), 0, 0);
}

static int pull_live(void) {
    unsigned scope;
    if (!st.pull_page) return 0;
    void *wm = window_manager();
    if (!usable() || window_manager_is_animating(wm) ||
        window_manager_get_top_window(wm) != st.pull_page)
        return 0;
    context_now(&scope);
    return scope == st.pull_scope;
}

static int pull_event(void *page, void *event) {
    if (page != st.pull_page) return 0;
    unsigned type = I(event, EVENT_TYPE);
    if (type == EVT_DESTROY || type == EVT_WINDOW_BACKGROUND || type == EVT_WINDOW_CLOSE ||
        type == EVT_POINTER_ABORT || !pull_live()) {
        pull_cancel();
        return 0;
    }
    int dx = I(event, EVENT_X) - st.pull_x;
    int dy = I(event, EVENT_Y) - st.pull_y;
    if (dx < 0) dx = -dx;
    int release = type == EVT_POINTER_UP_BEFORE;
    if (!st.pull_claimed) {
        if (release || (dx >= PULL_CLAIM_PX && dx >= dy) || dy <= -PULL_CLAIM_PX) {
            pull_cancel();
            return 0;
        }
        if (dy < PULL_CLAIM_PX) return 0;
        st.pull_claimed = 1;
        prop(st.pull_surface, PULL_SUPPRESS, 1);
        /* Abort the native pressed targets before swallowing move/up; native widgets clear
         * their pressed state and grabs. A canceled search must not turn into a row click. */
        unsigned abort_event[12];
        pointer_event_init(abort_event, EVT_POINTER_ABORT, page, st.pull_x, st.pull_y);
        widget_dispatch_event_to_target_recursive(page, abort_event);
        if (load_rows(&g_menu, st.pull_surface)) stop_scroll(&g_menu);
    }
    int ready = dy >= PULL_SEARCH_PX && dy > dx;
    if (release) {
        pull_cancel(); /* clear before stock search can navigate or destroy anything */
        if (ready) stock_search(page, event);
        return STOP;
    }
    void *prompt = widget_lookup(page, PULL_PROMPT, 1);
    if (!prompt) {
        prompt = label_create(page, 0, 0, I(page, W_W), 44);
        if (prompt) {
            widget_set_name(prompt, PULL_PROMPT);
            widget_use_style(prompt, "s_label_white18c");
            widget_set_enable(prompt, 0);
            widget_set_sensitive(prompt, 0);
            prop(prompt, "floating", 1);
            prop(prompt, "style:disable:bg_color", (int)0xff000000u);
            prop(prompt, "style:disable:text_color", (int)0xffffffffu);
        }
    }
    if (prompt) {
        widget_set_text_utf8(prompt, ready ? "Release to search" : "Pull to search");
        widget_set_visible(prompt, dy > 0, 0);
    }
    return STOP;
}

static void pull_begin(void *event) {
    pull_cancel();
    if (!event || I(event, EVENT_Y) < QUICK_EDGE_PX) return;
    void *page = window_manager_get_top_window(window_manager());
    if (!page) return;
    const char *name = widget_get_prop_str(page, "name", "");
    if (tk_strcmp(name, "localmusic_page")) return;
    void *w = surface((void *)0, (void *)0);
    if (!w) return;
    prop(w, PULL_SUPPRESS, 0);
    int xy[2] = { I(event, EVENT_X), I(event, EVENT_Y) };
    widget_to_local(w, xy);
    if (xy[0] < 0 || xy[1] < 0 || xy[0] >= I(w, W_W) || xy[1] >= I(w, W_H) ||
        I(w, kind(w) == 2 ? TABLE_TOP : SCROLL_Y) > 0)
        return;
    if (!widget_get_prop_int(page, PULL_BOUND, 0)) {
        if (!widget_on(page, EVT_DESTROY, pull_event, page) ||
            !widget_on(page, EVT_WINDOW_BACKGROUND, pull_event, page) ||
            !widget_on(page, EVT_WINDOW_CLOSE, pull_event, page) ||
            !widget_on(page, EVT_POINTER_ABORT, pull_event, page) ||
            !widget_on(page, EVT_POINTER_MOVE_BEFORE, pull_event, page) ||
            !widget_on(page, EVT_POINTER_UP_BEFORE, pull_event, page))
            return;
        prop(page, PULL_BOUND, 1);
    }
    if (!widget_get_prop_int(w, PULL_BOUND, 0)) {
        if (!widget_on(w, EVT_DESTROY, pull_event, page)) return;
        prop(w, PULL_BOUND, 1);
    }
    st.pull_page = page;
    st.pull_surface = w;
    st.pull_x = I(event, EVENT_X);
    st.pull_y = I(event, EVENT_Y);
    context_now(&st.pull_scope);
}
#else
#define pull_cancel() ((void)0)
#define pull_begin(event) ((void)0)
#endif

/* A one-digit setting under section in the stock config.ini, 0 to n - 1, else 0:
 * toolsReadConfig(path, section, key, out, default) copies the value, or the default. */
static int config_value(const char *section, const char *key, int n) {
    char s[256] = "";
    toolsReadConfig("/mnt/data/config.ini", section, key, s, "0");
    return s[0] >= '0' && s[0] < '0' + n && !s[1] ? s[0] - '0' : 0;
}

#if IPOD
/* 0xRRGGBB to an opaque color_t, whose bytes are r, g, b, a. */
#define RGBA(c) (0xff000000u | ((c) & 255) << 16 | ((c) & 0xff00) | (c) >> 16)

/* The 0xRRGGBB j/n of the way from one color to another, per channel. Shared with visualizer.c. */
unsigned mix(unsigned from, unsigned to, int j, int n) {
    unsigned c = 0;
    for (int s = 0; s < 24; s += 8)
        c |= (unsigned)(((int)(from >> s & 255) * (n - j) + (int)(to >> s & 255) * j) / n) << s;
    return c;
}

/* The Accent, Home and Battery settings (docs/ipod.md#display-settings), IPOD/ACCENT, HOME and
 * BATTERY in the stock config.ini: toolsReadConfig(path, section, key, out, default) copies the
 * value, or the default. */
static const unsigned accents[][5] = { ACCENTS };
#define ACCENT_N (int)(sizeof accents / sizeof *accents)
static const char *const accent_names[] = { "Accent: Graphite", "Accent: Crimson", "Accent: Tidal",
                                            "Accent: Champagne" };
_Static_assert(sizeof accent_names / sizeof *accent_names == ACCENT_N, "one name per ACCENTS row");
int config_digit(const char *key, int n) {
    return config_value("IPOD", key, n);
} /* and visualizer.c's */
static int accent(void) {
    if (!st.settings_read) {
        st.accent = config_digit("ACCENT", ACCENT_N);
        st.home_full = config_digit("HOME", 2);
        st.battery = config_digit("BATTERY", 3);
        st.settings_read = 1;
    }
    return st.accent;
}
int ipod_home_full(void) {
    accent();
    return st.home_full;
}
unsigned accent_tone(int tone) { return accents[accent()][tone]; }

/* A color_t (bytes r, g, b, a) that is stock red blended with a neutral, t * red + k * white per
 * channel within RED_TOLERANCE, becomes the same blend of one of the preset's tones (TONE_RED for
 * text and images, TONE_LIGHT for every other color): flat reds,
 * pressed tints and anti-aliased edges follow the accent, alpha is kept, and greys and other hues
 * are unchanged. t comes from least squares against red with the mean removed, in 1/4096. The white
 * part (k) becomes that share of glyph: white keeps a blend's light part as it is. With another
 * glyph, neutral pixels (the glyph itself) map too, so pass it only for an image that holds red.
 * Crimson is the identity. */
enum { TONE_LIGHT = 2, TONE_RED = 3 }; /* columns of ACCENTS */
static unsigned red_map(unsigned c, unsigned tone, unsigned glyph) {
    static const int red[3] = { STOCK_RED >> 16, STOCK_RED >> 8 & 255, STOCK_RED & 255 };
    const int sum = red[0] + red[1] + red[2];
    int ch[3] = { c & 255, c >> 8 & 255, c >> 16 & 255 }, s = ch[0] + ch[1] + ch[2], d = 0, dd = 0;
    for (int i = 0; i < 3; ++i) {
        d += (3 * ch[i] - s) * (3 * red[i] - sum);
        dd += (3 * red[i] - sum) * (3 * red[i] - sum);
    }
    int t = d * 4096 / dd;
    if (t < 256 && glyph != 0xffffff) t = 0; /* a neutral glyph pixel takes the glyph colour too */
    int k = (s * 4096 - t * sum) / (3 * 4096);
    if ((t && t < 256) || t > 4096 + 256 || k < -RED_TOLERANCE) return c;
    unsigned out = c & 0xff000000u;
    for (int i = 0; i < 3; ++i) {
        int miss = ch[i] - (t * red[i] / 4096 + k);
        if (miss > RED_TOLERANCE || miss < -RED_TOLERANCE) return c;
        int v = t * (int)(tone >> (16 - 8 * i) & 255) / 4096 +
                k * (int)(glyph >> (16 - 8 * i) & 255) / 255;
        out |= (unsigned)(v < 0 ? 0 : v > 255 ? 255 : v) << 8 * i;
    }
    return out;
}
unsigned accent_map(unsigned c, int preset, int tone) {
    return preset == CRIMSON ? c : red_map(c, accents[preset][tone], 0xffffff);
}

/* A vertical gradient in one-pixel bands, then a one-pixel top highlight; equal ends give a solid
 * fill. r.h is at least 2.
 * Plain fills keep this off the stock gradient_t ABI, which is not audited. */
static void gradient(void *canvas, rect_t r, unsigned top, unsigned bottom, unsigned hi) {
    for (int j = 0; j < r.h; ++j) {
        canvas_set_fill_color(canvas, RGBA(mix(top, bottom, j, r.h - 1)));
        canvas_fill_rect(canvas, r.x, r.y + j, r.w, 1);
    }
    canvas_set_fill_color(canvas, RGBA(hi));
    canvas_fill_rect(canvas, r.x, r.y, r.w, 1);
}
#endif

/* Narrow the canvas clip to the surface's viewport, keeping the old clip; false when none shows. */
static int clip_surface(void *canvas, menu_t *m, rect_t *old) {
    int clip[4];
    if (!clip_within(canvas, &old->x, clip, I(canvas, CANVAS_X), I(canvas, CANVAS_Y), I(m->w, W_W),
                     m->height))
        return 0;
    canvas_set_clip_rect(canvas, clip);
    return 1;
}

/* Library sorting ignores a leading "The ", "A " or "An " (Apple's rule) when a name follows it:
 * the length to skip, else 0. */
static unsigned article(const char *s) {
    for (const char *a = "the a an "; *a;) {
        unsigned n = 0;
        while (a[n] != ' ' && (s[n] | 32) == a[n]) ++n;
        if (a[n] == ' ' && s[n] == ' ' && (unsigned char)s[n + 1] > ' ') return n + 1;
        while (*a++ != ' ') {}
    }
    return 0;
}

/* Text centred in r in the default font at px, in color; text color and alignment are restored,
 * the font is not (stock sets it before its own text). Shared with peq_ui.c and visualizer.c. */
void draw_centred(void *canvas, const unsigned *s, unsigned n, const void *r, unsigned px,
                  unsigned color) {
    unsigned text = (unsigned)I(P(canvas, CANVAS_LCD), LCD_TEXT_COLOR);
    int align_v = I(canvas, CANVAS_ALIGN_V), align_h = I(canvas, CANVAS_ALIGN_H);
    canvas_set_font(canvas, (void *)0, px); /* the system default font */
    canvas_set_text_color(canvas, color);
    I(canvas, CANVAS_ALIGN_V) = I(canvas, CANVAS_ALIGN_H) = 1;
    canvas_draw_text_in_rect(canvas, s, n, r);
    I(canvas, CANVAS_ALIGN_V) = align_v;
    I(canvas, CANVAS_ALIGN_H) = align_h;
    canvas_set_text_color(canvas, text);
}

#if IPOD
/* A widget in a DRILL window (contexts.inc). Its own window decides, not the top one, so a window
 * painted during a transition keeps its own rows. */
static int drill(void *w) {
    w = window_of(w);
    int ctx = w ? context_id(widget_get_prop_str(w, "name", (void *)0)) : -1;
    return ctx >= 0 && (contexts[ctx].flags & DRILL);
}

/* The iPod `>` of each drill row, where stock rows place img_into, as stock hides its own
 * list_into: not in multi-select, and not on grid tiles. Home's rides its selection bar alone,
 * nudge included. The image manager caches the bitmap. */
static void paint_chevrons(void *w, void *canvas) {
    unsigned bitmap[64]; /* bitmap_t */
    rect_t old;
    void *win = window_of(w);
    int home = win && !tk_strcmp(widget_get_prop_str(win, "name", ""), "home_page");
    if (g_navbar_status || !kind(w) || !P(canvas, CANVAS_LCD) || !drill(w) ||
        (home && st.sel_w != w) || !load_rows(&g_menu, w) ||
        widget_load_image(w, "list_into", bitmap) || !clip_surface(canvas, &g_menu, &old))
        return;
    for (int i = 0; i < g_menu.n; ++i) {
        rect_t r = bounds(&g_menu, i);
        if (2 * r.w >= I(w, W_W) && (!home || i == st.sel_row))
            canvas_draw_icon(canvas, bitmap, r.x + r.w - CHEVRON_W + (int)bitmap[0] / 2,
                             home ? st.sel_y : r.y + r.h / 2); /* bitmap_t width @0 */
    }
    canvas_set_clip_rect(canvas, &old);
}

/* Writes v (under 1000) in decimal to s; returns the digits written. */
static unsigned put_num(unsigned *s, unsigned v) {
    unsigned n = 0;
    if (v >= 100) s[n++] = '0' + v / 100 % 10;
    if (v >= 10) s[n++] = '0' + v / 10 % 10;
    s[n++] = '0' + v % 10;
    return n;
}

static int letter_expire(const void *info) {
    (void)info;
    st.letter_timer = 0;
    void *w = surface((void *)0, (void *)0);
    if (w && w == st.wheel_surface) widget_invalidate_force(w, (void *)0);
    return 0;
}

/* iPod fast scroll: while the wheel ramp moves more than one row per step, the selected row's
 * first character sits in a dark translucent square over the list until LETTER_MS after the last
 * such step. A virtual table resolves the logical row in its recycled pool; an offscreen or
 * textless row shows nothing. The fill color and clip are restored. */
/* A rounded box in color, or a square one when the canvas declines to round it (no vgcanvas). */
static void fill_box(void *canvas, rect_t *r, unsigned color, unsigned radius) {
    if (canvas_fill_rounded_rect(canvas, r, (void *)0, &color, radius)) {
        canvas_set_fill_color(canvas, color);
        canvas_fill_rect(canvas, r->x, r->y, r->w, r->h);
    }
}

static void paint_letter(void *w, void *canvas) {
    rect_t old;
    if (!st.letter_timer || w != st.wheel_surface || st.wheel_run <= LIST_FIRST_MS ||
        st.touch_mode || !P(canvas, CANVAS_LCD) || !load_rows(&g_menu, w))
        return;
    int i = index_of(&g_menu, widget_get_prop_int(w, SEL, -1));
    const unsigned *s = i < 0 ? (void *)0 : row_id(g_menu.at[i]).title;
    while (s && *s == ' ') ++s;
    if (!s || !*s || !clip_surface(canvas, &g_menu, &old)) return;
    if (g_menu.ctx >= 0 && contexts[g_menu.ctx].kind == CTX_LOCAL) { /* the letter it sorts under */
        char head[8];
        unsigned k = 0;
        for (; k < 7 && s[k]; ++k) head[k] = (char)(s[k] < 128 ? s[k] : 127);
        head[k] = 0;
        s += article(head);
    }
    unsigned c = *s >= 'a' && *s <= 'z' ? *s - 32 : *s;
    unsigned fill = (unsigned)I(P(canvas, CANVAS_LCD), LCD_FILL_COLOR);
    rect_t box = { (I(w, W_W) - LETTER_BOX) / 2, (g_menu.height - LETTER_BOX) / 2, LETTER_BOX,
                   LETTER_BOX };
    fill_box(canvas, &box, (LETTER_ALPHA << 24) | FILL_RGB, LETTER_RADIUS);
    draw_centred(canvas, &c, 1, &box, LETTER_PX, 0xffffffff);
    canvas_set_fill_color(canvas, fill);
    canvas_set_clip_rect(canvas, &old);
}

/* The mode strip uses small drawn chevrons, not punctuation in the mode's text. */
static void paint_mode(void *w, void *canvas) {
    if (w != st.np_control || st.np_panel != 2 || !P(canvas, CANVAS_LCD)) return;
    int width = I(w, W_W), cy = I(w, W_H) / 2;
    if (width < 48 || cy < 6) return;
    unsigned fill = (unsigned)I(P(canvas, CANVAS_LCD), LCD_FILL_COLOR);
    canvas_set_fill_color(canvas, RGBA(0xffffff));
    for (int i = 0; i < 6; ++i) {
        canvas_fill_rect(canvas, 16 - i, cy - 6 + i, 2, 2);
        canvas_fill_rect(canvas, 16 - i, cy + 4 - i, 2, 2);
        canvas_fill_rect(canvas, width - 18 + i, cy - 6 + i, 2, 2);
        canvas_fill_rect(canvas, width - 18 + i, cy + 4 - i, 2, 2);
    }
    canvas_set_fill_color(canvas, fill);
}

/* A seek thumb distinguishes the wheel-controlled position from passive progress. */
static void paint_seek(void *w, void *canvas) {
    if (w != st.np_slider || st.np_panel != 1 || !P(canvas, CANVAS_LCD)) return;
    int max = widget_get_prop_int(w, "max", 0);
    int value = widget_get_prop_int(w, "value", 0), width = I(w, W_W);
    if (max <= 0 || width < 12) return;
    unsigned fill = (unsigned)I(P(canvas, CANVAS_LCD), LCD_FILL_COLOR);
    rect_t thumb = { (width - 12) * clamp_step(0, max, value) / max,
                     (I(w, W_H) - 12) / 2, 12, 12 };
    fill_box(canvas, &thumb, RGBA(0xffffff), 6);
    canvas_set_fill_color(canvas, fill);
}

/* Now Playing's art gets NP_ART_RADIUS corners, painted over it in the page's black: each corner
 * row outside the arc, then its edge pixel at the alpha of its uncovered part (1/16 px). */
static void paint_cover(void *w, void *canvas) {
    if (!w || w != st.np_cover || !P(canvas, CANVAS_LCD)) return;
    unsigned fill = (unsigned)I(P(canvas, CANVAS_LCD), LCD_FILL_COLOR);
    int r = NP_ART_RADIUS, ww = I(w, W_W), h = I(w, W_H);
    for (int i = 0; i < r; ++i) {
        /* s is 16 sqrt(r * r - d * d / 4), the arc's half-width at this row's centre */
        int d = 2 * (r - i) - 1, v = (4 * r * r - d * d) * 64, s = 0;
        while ((s + 1) * (s + 1) <= v) ++s;
        int out = 16 * r - s, n = out >> 4;
        for (int c = 0; c < 4; ++c) {
            int y = c & 1 ? h - 1 - i : i, left = !(c & 2);
            canvas_set_fill_color(canvas, RGBA(0));
            canvas_fill_rect(canvas, left ? 0 : ww - n, y, n, 1);
            canvas_set_fill_color(canvas, (unsigned)(out & 15) * 17 << 24);
            canvas_fill_rect(canvas, left ? n : ww - n - 1, y, 1, 1);
        }
    }
    canvas_set_fill_color(canvas, fill);
}
#endif

static int field_count(void *page) {
    const char *name = page ? widget_get_prop_str(page, "name", "") : "";
    return !tk_strcmp(name, "manualtime_page") ? 5 : !tk_strcmp(name, "sleepshutdown_page") ? 2 : 0;
}

static void *field_target(void *page, int field) {
    static const char *const names[] = { "tselector_year", "tselector_month", "tselector_day",
                                        "tselector_hour", "tselector_min", "btn_enter" };
    int count = field_count(page);
    if (!count || field < 0 || field > count) return (void *)0;
    int index = field == count ? 5 : count == 2 ? field + 3 : field;
    return widget_lookup(page, names[index], 1);
}

/* A native swipe between Date and Time moves wheel ownership to that page's first field. */
static int field_index(void *page) {
    int count = field_count(page), field = widget_get_prop_int(page, "_wheel_field", 0);
    field = clamp_step(field, count, 0);
    if (count == 5 && field < count) {
        void *slide = widget_lookup(page, "slide_view", 1);
        int time = slide && widget_get_prop_int(slide, "value", 0) == 1;
        if (time != (field >= 3)) field = time ? 3 : 0;
    }
    prop(page, "_wheel_field", field);
    return field;
}

/* Draw at the control's own origin, including controls outside a list's clipping rectangle. */
static void paint_control(void *w, void *canvas) {
    void *top = window_manager_get_top_window(window_manager()), *target = (void *)0;
    if (!w || !canvas || !usable() || !top || !P(canvas, CANVAS_LCD)) return;
    if (field_count(top))
        target = field_target(top, field_index(top));
    else if (!tk_strcmp(widget_get_prop_str(top, "name", ""), "specfolder_page")) {
        void *table = widget_lookup(top, "specfolder_table_client", 1);
        if (table && widget_get_prop_int(table, SEL, -1) == I(table, TABLE_ROWS))
            target = widget_lookup(top, "btn_startscan", 1);
    }
    if (target != w || !control_available(w, top)) return;
    int width = I(w, W_W), height = I(w, W_H);
    if (width < 5 || height < 5) return;
    unsigned old = (unsigned)I(P(canvas, CANVAS_LCD), LCD_STROKE_COLOR);
    canvas_set_stroke_color(canvas, 0xffffffff);
    canvas_stroke_rect(canvas, 1, 1, width - 2, height - 2);
    canvas_stroke_rect(canvas, 2, 2, width - 4, height - 4);
    canvas_set_stroke_color(canvas, old);
}

/* Load and settle the painted surface's selection, then draw it: a neutral outline over the rows
 * in Stock, a full-width accent bar behind them in iPod.
 * The outline is one neutral white line seated on a dark shade line: the shade is the stock dark
 * surface at an alpha high enough to hold the white over bright album art, and being the same
 * color as the dark rows it vanishes on the stock theme. The translucent fill keeps the row
 * readable over artwork without borrowing the red "playing" language or the native focus flag.
 * Small rows and degenerate geometry keep the square fallback. */
static void paint_selection(void *w, void *canvas) {
#if IPOD
    if (w == st.sel_w) st.sel_w = (void *)0;
#endif
    if (!w || !canvas || !kind(w)) return;
    void *top = window_manager_get_top_window(window_manager());
    int i, shown = !st.touch_mode;
#if IPOD
    void *own = window_of(w);
    if (own && own != top && slides(top)) {
        /* The page under a sliding one, painted into the animator's snapshot: draw the row it
         * holds. Loading, recall and settling read the top page's state, so none of it runs.
         * A pending untouch is a Return by button, which shows the row of the page behind. */
        if (!usable() || !load_rows(&g_menu, w)) return;
        i = index_of(&g_menu, widget_get_prop_int(w, SEL, -1));
        top = own;
        if (st.untouch_timer) shown = 1;
    } else
#endif
    {
        if (surface((void *)0, (void *)0) != w) return;
        if (!load(&g_menu, w, !window_manager_get_pointer_pressed(window_manager()))) {
            cancel_center();
            return;
        }
        if (st.center_timer && !pending_matches(top, &g_menu)) cancel_center();
        if (g_menu.kind == 3) return; /* Home shows its selected card. */
        i = reconcile(&g_menu,
                      !moving(&g_menu) && !window_manager_get_pointer_pressed(window_manager()));
    }
#if IPOD
    if (selection_window(top)) shown = 1;
#endif
    /* Browsing pages hide the selection after touch; option pickers always show it. */
    int home = top && !tk_strcmp(widget_get_prop_str(top, "name", ""), "home_page");
    if (i < 0 || (!shown && !home)) return;
    if (footer_row(&g_menu, g_menu.id[i])) return; /* painted at the footer's own origin */
    rect_t r = bounds(&g_menu, i), old;
    /* A boundary detent nudges the selection against the end until it springs back. */
    if (fx_live(w) && st.bump_dir) r.y -= st.bump_dir * BUMP_PX;
    if (r.w < 5 || r.h < 5 || !P(canvas, CANVAS_LCD)) return;
    void *lcd = P(canvas, CANVAS_LCD);
    unsigned fill_color = (unsigned)I(lcd, LCD_FILL_COLOR);
    unsigned stroke_color = (unsigned)I(lcd, LCD_STROKE_COLOR);
    if (!clip_surface(canvas, &g_menu, &old)) return;
#if IPOD
    /* A list row gets the full surface width; a grid tile keeps its own rect. */
    if (2 * r.w >= I(g_menu.w, W_W)) {
        r.x = 0;
        r.w = I(g_menu.w, W_W);
    }
    const unsigned *a = accents[accent()];
    gradient(canvas, r, a[0], a[1], a[4]);
    st.sel_w = w;
    st.sel_row = i;
    st.sel_y = r.y + r.h / 2;
    if (g_menu.kind == 4 &&
        r.w < I(g_menu.w, W_W)) { /* a pop-up button's tile: framed white on any accent */
        canvas_set_stroke_color(canvas, 0xffffffff);
        canvas_stroke_rect(canvas, r.x, r.y, r.w, r.h);
        canvas_stroke_rect(canvas, r.x + 1, r.y + 1, r.w - 2, r.h - 2);
    }
#else
    rect_t outer = { r.x + 1, r.y + 1, r.w - 2, r.h - 2 };
    rect_t inner = { outer.x + 1, outer.y + 1, outer.w - 2, outer.h - 2 };
    int drawn = 0;
    /* Two concentric one-pixel rounded strokes, shade outside and white inside, not one
     * border_width=2 call: the effect then does not depend on how a canvas backend
     * interprets the width argument. Geometry is checked before the call, and radius 9/8
     * both stay above the stock "square at <= 2" cutoff. The stock rounded stroke returns
     * non-zero when its backend cannot draw (for example a canvas without a vgcanvas), and
     * the safe square fallback then keeps the outline visible. */
    if (outer.w > 2 * RADIUS && outer.h > 2 * RADIUS) {
        unsigned fill = FILL_COLOR;
        canvas_fill_rounded_rect(canvas, &outer, (void *)0, &fill, RADIUS);
        unsigned shade = SHADE_COLOR;
        unsigned white = OUTLINE_COLOR;
        drawn = canvas_stroke_rounded_rect(canvas, &outer, (void *)0, &shade, RADIUS, 1) == 0;
        if (drawn)
            drawn =
                canvas_stroke_rounded_rect(canvas, &inner, (void *)0, &white, RADIUS - 1, 1) == 0;
    }
    if (!drawn) {
        canvas_set_stroke_color(canvas, SHADE_COLOR);
        canvas_stroke_rect(canvas, outer.x, outer.y, outer.w, outer.h);
        canvas_set_stroke_color(canvas, OUTLINE_COLOR);
        canvas_stroke_rect(canvas, inner.x, inner.y, inner.w, inner.h);
    }
#endif
    /* Save/restore explicitly: this firmware's canvas_save/restore cover neither clip nor
     * either color, and the global alpha is deliberately never touched. */
    canvas_set_fill_color(canvas, fill_color);
    canvas_set_stroke_color(canvas, stroke_color);
    canvas_set_clip_rect(canvas, &old);
}

/* Stock paints children first and calls this with the surface's canvas origin restored. */
int ringnav_paint(void *w, void *canvas) {
#if IPOD
    if (st.pull_page && !pull_live()) pull_cancel();
#endif
    int result = stock_paint_trampoline(w, canvas);
    paint_control(w, canvas);
    coverflow_paint(w, canvas);
    photos_paint(w, canvas);
    books_paint(w, canvas);
    peq_paint(w, canvas);
#if IPOD
    visualizer_paint(w, canvas);
#endif
    /* Even a page with no navigable pane must end pending input when it is painted. */
    if (st.center_timer || st.home_surface) {
        void *wm = window_manager(), *top = window_manager_get_top_window(wm);
        if (!usable() || window_manager_is_animating(wm) || top != st.center_top) cancel_center();
        if (!carousel_page(top)) st.home_surface = (void *)0;
    }
#if IPOD
    paint_chevrons(w, canvas);
    paint_letter(w, canvas);
    paint_cover(w, canvas);
    paint_seek(w, canvas);
    paint_mode(w, canvas);
    coverflow_home_clip(w, canvas, 0);
#else
    paint_selection(w, canvas);
#endif
    return result;
}

/* A s_listitem_black list_item in view with a 335x70 s_btn_listitem button; returns the button. */
static void *list_button(void *view) {
    void *item = list_item_create(view, 0, 0, 0, 0);
    widget_use_style(item, "s_listitem_black");
    void *button = button_create(item, 20, 0, 335, 70);
    widget_use_style(button, "s_btn_listitem");
    return button;
}

/* A stock settings-style row (0x4c19bc): a list_button with a 52px icon and a 24px label at x 72,
 * without list_into. Returns the label. */
static void *list_row(void *view, const char *icon, int (*click)(void *, void *), void *ctx) {
    void *button = list_button(view);
    widget_on(button, EVT_CLICK, click, ctx);
    if (icon) image_base_set_image(image_create(button, 10, 0, SET_STOCK_ICON, 70), icon);
    void *label = hscroll_label_create(button, icon ? 72 : 10, 0, icon ? 260 : 322, 70);
    widget_use_style(label, "s_scrlabel_white24l");
    set_hscroll_label_attribute(label);
    return label;
}

/* The stock player picker uses mode 2 to add its selected queue song.
 * Mode 0 opens the same page for browsing, rename and delete. Keep More
 * underneath so Back returns here without closing a window inside its click dispatch. */
static int player_playlists(void *win, void *event) {
    (void)win;
    (void)event;
    navigator_to_with_context("localmusic/playlist_page", (void *)0);
    return 0;
}

int ringnav_playermore(void *win, void *ctx) {
    int result = stock_playermore_trampoline(win, ctx);
    void *view = win ? widget_lookup(win, "scroll_view_more", 1) : (void *)0;
    if (view && widget_count_children(view) == 8) {
        void *label = list_row(view, (void *)0, player_playlists, win);
        widget_set_text_utf8(label, "Manage playlists");
        widget_restack(P(P(label, W_PARENT), W_PARENT), 2);
    }
    return result;
}

/* iPod hides the navbar's Create button. Put the same action in the navigable list in every
 * mode, including an empty picker. Hook the rebuild so it survives returning from naming.
 * Stock playlist buttons keep their names and indices after the new row is inserted. */
int ringnav_playlist_rows(void *win) {
    int result = stock_playlist_rows_trampoline(win);
    void *view = win ? widget_lookup(win, "scroll_view", 1) : (void *)0;
    if (view) {
        void *label = list_row(view, (void *)0, playlist_create, win);
        widget_set_text_utf8(label, "Create playlist");
        widget_restack(P(P(label, W_PARENT), W_PARENT), 0);
    }
    return result;
}

#if IPOD
/* Current values share one right column. Stock labels keep their identities and callbacks. */
static void setting_pair_layout(void *title, void *value) {
    void *button = P(title, W_PARENT);
    int x = I(title, W_X), h = I(button, W_H), edge = I(button, W_W) - SET_EDGE;
    widget_move_resize(title, x, 0, edge - 120 - 12 - x, h);
    widget_move_resize(value, edge - 120, 0, 120, h);
    prop(title, "style:normal:font_size", 20);
    prop(value, "style:normal:font_size", 16);
    widget_set_prop_str(value, "style:normal:text_align_h", "right");
    for (unsigned j = 0; j < widget_count_children(button); j++) {
        void *c = widget_get_child(button, j);
        if (!tk_strcmp(widget_get_type(c), "image") &&
            !tk_strcmp(widget_get_prop_str(c, "image", ""), "list_into"))
            widget_set_visible(c, 0, 0);
    }
}

static void setting_pair(void *title, const char *caption, const char *current) {
    if (!title) return;
    void *button = P(title, W_PARENT);
    void *value = (void *)(unsigned)widget_get_prop_int(title, "_setting_value", 0);
    if (!value) {
        value = hscroll_label_create(button, 176, 0, 159, 70);
        if (!value) return;
        widget_use_style(value, "s_scrlabel_white20r");
        set_hscroll_label_attribute(value);
        prop(title, "_setting_value", (int)value);
    }
    widget_set_text_utf8(title, caption);
    widget_set_text_utf8(value, current);
    if (I(button, W_W) == 375) setting_pair_layout(title, value);
}

/* The payload's settings used to put both parts in one label, e.g. "Wake: Double click". */
static void setting_pair_text(void *label, const char *s) {
    char caption[64];
    unsigned n = 0;
    while (s[n] && s[n] != ':' && n < sizeof caption - 1) { caption[n] = s[n]; ++n; }
    caption[n] = 0;
    if (s[n] != ':') return;
    s += n + 1;
    while (*s == ' ') ++s;
    setting_pair(label, caption, s);
}
#endif

/* Power management's Charge limit, Low power and Wake use Q2POD CHARGELIMIT, LOWPOWER and
 * SINGLEWAKE in config.ini. Audio settings' Artists uses stock's own
 * PLAYSET ARTISTTYPE (artist_type, the artist page's switch; docs/internals.md#album-artists). */
#define STR_(x) #x
#define STR(x) STR_(x)
enum { POD_CHARGE, POD_LOW, POD_WAKE, POD_ARTISTS };
static void pod_settings(void) {
    if (st.pod_read) return;
    st.charge_limit = config_value("Q2POD", "CHARGELIMIT", 2);
    st.low_power = config_value("Q2POD", "LOWPOWER", 2);
    st.single_wake = config_value("Q2POD", "SINGLEWAKE", 2);
    st.pod_read = 1;
}
static void pod_text(int i) {
    static const char *const names[][2] = {
        { "Charge limit: Off", "Charge limit: " STR(CHARGE_STOP) "%" },
        { "Low power: Off", "Low power: On" },
        { "Wake: Double click", "Wake: Single click" },
        { "Artists: Artist", "Artists: Album Artist" },
    };
    int v = i == POD_CHARGE ? st.charge_limit
            : i == POD_LOW  ? st.low_power
            : i == POD_WAKE ? st.single_wake
                            : I(artist_type, 0) == 1;
#if IPOD
    setting_pair_text(st.pod_label[i], names[i][v]);
#else
    widget_set_text_utf8(st.pod_label[i], names[i][v]);
#endif
}
/* Centre or tap toggles and saves. Wake takes effect immediately; Charge limit and Low power on
 * the next UI loop pass (power_poll); Artists on the next load of an artist list. */
static int pod_click(void *ctx, void *event) {
    (void)event;
    int i = (int)(long)ctx;
    pod_settings();
    if (i == POD_ARTISTS) {
        int v = I(artist_type, 0) != 1;
        I(artist_type, 0) = v;
        write_int_config(v, "PLAYSET", "ARTISTTYPE");
    } else {
        int *value = i == POD_LOW ? &st.low_power
                     : i == POD_WAKE ? &st.single_wake : &st.charge_limit;
        *value = !*value;
        write_int_config(*value, "Q2POD", i == POD_LOW ? "LOWPOWER"
                         : i == POD_WAKE ? "SINGLEWAKE" : "CHARGELIMIT");
        if (i == POD_WAKE) st.unlock_waiting = 0;
        st.charge_at = 0;
    }
    pod_text(i);
    return 0;
}
static void pod_rows(void *win, const char *view_name, int first, int n, const char *const *icons) {
    void *view = win ? widget_lookup(win, view_name, 1) : (void *)0;
    pod_settings();
    for (int i = 0; view && i < n; ++i) {
        st.pod_label[first + i] = list_row(view, icons[i], pod_click, (void *)(long)(first + i));
        pod_text(first + i);
    }
}

/* Reuse stock's Centre-hold confirmation and its power-state guards without a physical hold. */
static int shutdown_click(void *ctx, void *event) {
    (void)ctx; (void)event;
    unsigned key_event[EVENT_KEY / sizeof(unsigned) + 1] = { 0 };
    I(key_event, EVENT_TYPE) = EVT_KEY_LONG;
    I(key_event, EVENT_KEY) = KEY_CENTER;
    return stock_keylong_trampoline((void *)0, key_event);
}

/* systemset_powermanager_page_init and playset_playset_page_init: stock builds its rows, then
 * these follow in the same widgets and styles (list_row), with the value in the label. */
int ringnav_powermanager(void *win, void *ctx) {
    static const char *const icons[] = { "usb_chargeswitch", "system_powermanager", "system_keylock" };
    int result = stock_power_trampoline(win, ctx);
    pod_rows(win, "scroll_view_powermanager", POD_CHARGE, 3, icons);
    void *view = win ? widget_lookup(win, "scroll_view_powermanager", 1) : (void *)0;
    if (view)
        widget_set_text_utf8(list_row(view, "system_powermanager", shutdown_click, (void *)0), "Shut down");
    return result;
}
int ringnav_audioset(void *win, void *ctx) {
    static const char *const icons[] = { "playset_folderjump" };
    int result = stock_audioset_trampoline(win, ctx);
    pod_rows(win, "scroll_view_playset", POD_ARTISTS, 1, icons);
    return result;
}

#if IPOD
/* The status bar's centred label shows the device's local time as 6:14 PM: 12-hour, without
 * seconds or a leading zero, or --:-- when the time cannot be read. The bar repaints at least once
 * a second (systembar_showface), and the label is written only when the minute shown changes.
 * struct tm: tm_min @4, tm_hour @8. */
static void clock_sync(void *bar) {
    long now = time((void *)0);
    const int *tm = now == -1 ? (void *)0 : localtime(&now);
    int ok = tm && tm[1] >= 0 && tm[1] < 60 && tm[2] >= 0 && tm[2] < 24;
    unsigned key = ok ? (unsigned)(tm[2] * 60 + tm[1]) + 1 : ~0u; /* 0 before the first */
    void *label = key == st.clock_key ? (void *)0 : widget_lookup(bar, "label_clock", 1);
    if (!label) return;
    st.clock_key = key;
    char s[16] = "--:--";
    if (ok)
        tk_snprintf(s, sizeof s, "%d:%02d %s", (tm[2] + 11) % 12 + 1, tm[1],
                    tm[2] < 12 ? "AM" : "PM");
    widget_set_text_utf8(label, s);
}

/* systembar_showface sets img_bt's image each second: a codec badge (BT_CODECS) while stock shows
 * the codec, else bar_bt or BT_GLYPH. A new badge shows CODEC_MS, then codec_fade fades it out and
 * BT_GLYPH in, and bar_sync keeps putting BT_GLYPH back over stock's badge before img_bt paints. */
static const char *const codecs[] = BT_CODECS;
static const int codec_reach[] = BT_CODEC_REACH;
_Static_assert(sizeof codecs / sizeof *codecs == sizeof codec_reach / sizeof *codec_reach,
               "one reach per codec");

static int codec_fade(const void *info) {
    (void)info;
    int n = ++st.codec_step, d = CODEC_STEPS - n;
    if (n == 1)
        st.codec_timer = timer_add(codec_fade, (void *)0, CODEC_STEP_MS); /* this one ends */
    if (n == CODEC_STEPS) image_base_set_image(st.bar_bt, BT_GLYPH);
    widget_set_opacity(st.bar_bt, 255u * (unsigned)(d < 0 ? -d : d) / CODEC_STEPS);
    if (n < 2 * CODEC_STEPS) return n == 1 ? 0 : 8; /* RET_REPEAT */
    st.codec_timer = 0;
    return 0;
}

/* The status bar's codec badge, then the Battery setting (docs/ipod.md#status-bar-and-clock): one
 * of the stock icon, stock's percentage or the payload's battery (view_battery) shows, and the icon
 * wherever the group's ink would reach more than BATT_ROOM (a wide badge while it shows).
 * widget_set_visible does nothing for an unchanged state and relayouts the view for a new one. */
static void bar_sync(void *bar) {
    if (!st.bar_bt) {
        st.bar_bt = widget_lookup(bar, "img_bt", 1);
        st.bar_wifi = widget_lookup(bar, "img_wifi", 1);
        st.bar_pct = widget_lookup(bar, "label_battery", 1);
        st.bar_slot = widget_lookup(bar, "view_battery", 1);
        st.bar_icon = widget_lookup(bar, "img_battery", 1);
    }
    if (!st.bar_bt || !st.bar_wifi || !st.bar_pct || !st.bar_slot || !st.bar_icon) return;
    int shown = widget_get_visible(st.bar_bt), c = 0;
    const char *image = shown ? widget_get_prop_str(st.bar_bt, "image", "") : "";
    for (int i = 0; image && i < (int)(sizeof codecs / sizeof *codecs); ++i)
        if (!tk_strcmp(image, codecs[i])) c = i + 1;
    /* A new badge starts over; BT_GLYPH with a badge is the faded badge itself. */
    if (c != st.codec && (c || !image || tk_strcmp(image, BT_GLYPH))) {
        stop_timer(&st.codec_timer);
        st.codec = c;
        st.codec_step = 0;
        widget_set_opacity(st.bar_bt, 255);
        if (c) st.codec_timer = timer_add(codec_fade, (void *)0, CODEC_MS);
    } else if (c && st.codec_step >= CODEC_STEPS)
        image_base_set_image(st.bar_bt, BT_GLYPH);
    accent(); /* reads the settings */
    int mode = st.battery;
    if (mode) {
        int reach = mode == 1 ? BATT_PCT_W : BATT_BODY_W + BATT_NUB_W;
        if (widget_get_visible(st.bar_wifi)) reach += I(st.bar_wifi, W_W) + 5;
        if (shown)
            reach += 5 + (st.codec && st.codec_step < CODEC_STEPS ? codec_reach[st.codec - 1]
                                                                  : BT_REACH);
        if (reach > BATT_ROOM) mode = 0;
    }
    widget_set_visible(st.bar_icon, !mode, 0);
    widget_set_visible(st.bar_pct, mode == 1, 0);
    widget_set_visible(st.bar_slot, mode == 2, 0);
    if (mode != 2) return;
    /* the level from label_battery's "88%" (stock zeroes progress_battery while charging), then
     * charging (bar_charge) and low (bar_lowcharge), as stock picks the icon */
    const char *icon = widget_get_prop_str(st.bar_icon, "image", "");
    const unsigned *t = widget_get_text(st.bar_pct);
    int level = 0;
    while (t && *t >= '0' && *t <= '9') level = level * 10 + (int)(*t++ - '0');
    unsigned key = (unsigned)(level < 0     ? 0
                              : level > 100 ? 100
                                            : level)
                       << 2 |
                   (unsigned)(icon && !tk_strcmp(icon, "bar_charge")) << 1 |
                   (unsigned)(icon && !tk_strcmp(icon, "bar_lowcharge"));
    if (key == st.batt_key) return;
    st.batt_key = key;
    widget_invalidate_force(st.bar_slot, (void *)0);
}

/* view_battery: a BATT_BODY_W x BATT_BODY_H outline with square-cut corners, centred in the bar
 * (its text 1px low), its nub on the right and the level inside, all in one colour; the fill color
 * is restored. */
static void paint_battery(void *w, void *canvas) {
    void *lcd = P(canvas, CANVAS_LCD);
    if (!lcd) return;
    unsigned fill = (unsigned)I(lcd, LCD_FILL_COLOR);
    unsigned key = st.batt_key, level = key >> 2, s[3], n = put_num(s, level);
    unsigned color = RGBA(key & 2   ? BATT_CHARGE_RGB
                          : key & 1 ? accents[accent()][TONE_RED]
                                    : 0xffffff);
    const int bw = BATT_BODY_W, bh = BATT_BODY_H, y = (I(w, W_H) + 1 - bh) / 2;
    canvas_set_fill_color(canvas, color);
    canvas_fill_rect(canvas, 1, y, bw - 2, 1);
    canvas_fill_rect(canvas, 1, y + bh - 1, bw - 2, 1);
    canvas_fill_rect(canvas, 0, y + 1, 1, bh - 2);
    canvas_fill_rect(canvas, bw - 1, y + 1, 1, bh - 2);
    canvas_fill_rect(canvas, bw, y + (bh - BATT_NUB_H) / 2, BATT_NUB_W, BATT_NUB_H);
    rect_t r = { 0, y + 1, bw, bh };
    draw_centred(canvas, s, n, &r, BATT_PX, color);
    canvas_set_fill_color(canvas, fill);
}

/* Seconds as stock writes label_playtime, after a minus when negative is 1. */
static void clock_text(void *label, int negative, int t) {
    char s[16] = "-";
    toolsTimeItoa(s + negative, t);
    widget_set_text_utf8(label, s);
}

/* The Classic-style bottom band is flat: a progress bar, seek thumb, or centred mode label. */
static void np_controls(void) {
    if (!st.np_control || !st.np_slider) return;
    int modes = st.np_panel == 2;
    widget_set_visible(st.np_slider, !modes, 0);
    if (st.np_elapsed) widget_set_visible(st.np_elapsed, !modes, 0);
    if (st.np_remain) widget_set_visible(st.np_remain, !modes, 0);
    widget_set_visible(st.np_control, st.np_panel != 0, 0);
    if (st.np_panel == 1 && st.np_elapsed) {
        widget_set_prop_int(st.np_control, "style:normal:font_size", NP_TIMES_PX);
        widget_move_resize(st.np_control, 100, I(st.np_elapsed, W_Y),
                           I(st.np_win, W_W) - 200, I(st.np_elapsed, W_H));
        widget_set_text_utf8(st.np_control, "Seek");
    } else if (modes) {
        widget_set_prop_int(st.np_control, "style:normal:font_size", 20);
        static const char *const names[] = {
            "List play", "Repeat one", "Shuffle songs", "Repeat all"
        };
        int mode = *(volatile int *)MCL_MODE;
        widget_move_resize(st.np_control, I(st.np_slider, W_X), I(st.np_slider, W_Y),
                           I(st.np_slider, W_W), I(st.np_slider, W_H));
        widget_set_text_utf8(st.np_control, names[mode >= 0 && mode < 4 ? mode : 0]);
        st.np_mode = mode;
    }
    widget_invalidate_force(st.np_win, (void *)0);
}

/* Now Playing's "3 of 12", album and remaining time (slider max less value, stock's seconds, so a
 * drag previews it). Labels are written only when their source changes. */
static void np_sync(void *top) {
    if (!top || top != st.np_win) return;
    if (st.np_panel == 2 && st.np_mode != *(volatile int *)MCL_MODE) np_controls();
    unsigned at, n;
    void *r = queue_now(&at, &n);
    const char *album = now_tag(r, REC_ALBUM);
    char s[24] = "";
    unsigned pos[2] = { at, n };
    unsigned h = hash_bytes(fnv(FNV_SEED, (const unsigned char *)album), (const unsigned char *)pos,
                            sizeof pos);
    if (h != st.np_hash) {
        st.np_hash = h;
        if (r) tk_snprintf(s, sizeof s, "%d of %d", at + 1, n);
        widget_set_text_utf8(st.np_pos, s);
        widget_set_text_utf8(st.np_album, album ? album : "");
    }
    int left =
        widget_get_prop_int(st.np_slider, "max", 0) - widget_get_prop_int(st.np_slider, "value", 0);
    if (left < 0) left = 0;
    if (left == st.np_left) return;
    st.np_left = left;
    clock_text(st.np_remain, 1, left);
}

/* The accent's light tone fills the progress bar. */
static void np_fill(void) {
    if (!st.np_slider) return;
    unsigned color = RGBA(accents[accent()][TONE_LIGHT]);
    widget_set_prop_int(st.np_slider, "style:normal:fg_color", (int)color);
    widget_invalidate_force(st.np_slider, (void *)0);
}

/* The lyrics: stock's 250 ms timer scrolls scroll_lrc back to the current line on every tick, so
 * it stops while the wheel scrolls them and restarts SCRUB_MS after the last tick. */
static void lyric_end(void) {
    if (!st.lyric_timer) return;
    stop_timer(&st.lyric_timer);
    if (st.np_win) playing_timer_start(st.np_win);
}

static int lyric_expire(const void *info) {
    (void)info;
    st.lyric_timer = 0;
    if (st.np_win) playing_timer_start(st.np_win);
    return 0;
}

/* The wheel on the lyrics page (the slide_view's second), while the track has lyrics, scrolls
 * them LYRIC_STEP a tick ahead of the volume. */
static int np_lyrics(unsigned key) {
    void *lrc = st.np_lrc;
    if (!st.np_slide || !lrc || widget_get_prop_int(st.np_slide, "value", 0) != 1 ||
        !widget_count_children(lrc) || mclGetLyricSize() <= 0)
        return 0;
    if (!st.lyric_timer) playing_timer_clear(st.np_win);
    rearm(&st.lyric_timer, lyric_expire, SCRUB_MS);
    int y = clamp_step(I(lrc, SCROLL_Y), I(lrc, VIEW_CONTENT_H) - I(lrc, W_H),
                       key == KEY_NEXT ? LYRIC_STEP : -LYRIC_STEP);
    scroll_view_set_offset(lrc, I(lrc, SCROLL_X), y);
    return 1;
}

static void np_cancel(void) {
    stop_timer(&st.np_press);
    if (st.np_seek_timer) {
        stop_timer(&st.np_seek_timer);
        if (st.np_win) playing_timer_start(st.np_win);
    }
    lyric_end();
}

/* The native seek may block, so rapid wheel ticks preview locally and seek once after settling.
 * A changed queue/track rejects the pending target rather than seeking the next song. */
static unsigned np_track_key(void) {
    unsigned at, n;
    void *r = queue_now(&at, &n);
    return r ? hash_bytes(fnv(FNV_SEED, P(r, REC_PATH)), (const unsigned char *)r + REC_CUE_START, 8) : 0;
}

static int np_seek(const void *info) {
    (void)info;
    st.np_seek_timer = 0;
    if (!st.np_win) return 0;
    if (usable() && window_manager_get_top_window(window_manager()) == st.np_win &&
        st.np_seek_queue == P(mcl_pdeqplaylist, 0) &&
        st.np_seek_pos == *(volatile int *)MCL_POS && st.np_seek_hash == np_track_key())
        player_seek_time(st.np_seek_value);
    playing_timer_start(st.np_win);
    return 0;
}

static int np_wheel(void *top, unsigned key) {
    if (!st.np_panel || (key != KEY_PREV && key != KEY_NEXT)) return 0;
    /* The caller also accepts Now Playing's volume overlay. It cannot own the wheel here. */
    if (top != st.np_win) window_close(top);
    stop_timer(&st.np_press);
    int dir = key == KEY_NEXT ? 1 : -1;
    if (st.np_panel == 2) {
        int mode = *(volatile int *)MCL_MODE;
        config_playmode(mode >= 0 && mode < 4 ? (mode + dir + 4) % 4 : 0, 1);
        np_controls();
        return 1;
    }
    if (!st.np_slider) return 1;
    int max = widget_get_prop_int(st.np_slider, "max", 0);
    if (max <= 0) return 1;
    if (st.np_seek_timer && (st.np_seek_queue != P(mcl_pdeqplaylist, 0) ||
                            st.np_seek_pos != *(volatile int *)MCL_POS || st.np_seek_hash != np_track_key())) {
        np_cancel();
        return 1;
    }
    int value = widget_get_prop_int(st.np_slider, "value", 0);
    int next = clamp_step(value, max, dir * NP_SEEK_STEP);
    if (value == next) return 1;
    lyric_end();
    if (!st.np_seek_timer) playing_timer_clear(st.np_win);
    st.np_seek_queue = P(mcl_pdeqplaylist, 0);
    st.np_seek_pos = *(volatile int *)MCL_POS;
    st.np_seek_hash = np_track_key();
    st.np_seek_value = next;
    st.np_seek_changed = 1;
    widget_set_prop_int(st.np_slider, "value", next);
    clock_text(st.np_elapsed, 0, next);
    np_sync(st.np_win);
    rearm(&st.np_seek_timer, np_seek, NP_SEEK_MS);
    if (!st.np_seek_timer) np_seek((void *)0);
    return 1;
}

/* A single Centre press changes the bottom control, never the playback mode itself. */
static int np_single(const void *info) {
    (void)info;
    st.np_press = 0;
    if (!st.np_win) return 0;
    void *wm = window_manager(), *top = window_manager_get_top_window(wm);
    unsigned n = widget_count_children(wm);
    if (top != st.np_win && top && n >= 2 && widget_get_child(wm, n - 2) == st.np_win &&
        !tk_strcmp(widget_get_prop_str(top, "name", ""), "volume_dialog"))
        window_close(top);
    else if (top != st.np_win || !usable()) {
        np_cancel();
        return 0;
    }
    if (st.np_seek_timer) {
        stop_timer(&st.np_seek_timer);
        np_seek((void *)0);
    }
    st.np_panel = st.np_panel == 1 && st.np_seek_changed ? 0 : (st.np_panel + 1) % 3;
    st.np_seek_changed = 0;
    np_controls();
    return 0;
}

/* One Centre cycles progress, seek and modes. A double click keeps the stock screen-off path. */
static int np_key(unsigned key) {
    unsigned now = (unsigned)time_now_ms();
    if (key != KEY_CENTER) return 0;
    if (st.np_press) {
        stop_timer(&st.np_press);
        if (now - st.np_press_at < DOUBLE_CLICK_MS) {
            static const unsigned release[EVENT_KEY / 4 + 1] = { [EVENT_KEY / 4] = KEY_CENTER };
            np_cancel();
            if (g_backlight_status) on_wm_keyup_fun((void *)0, (void *)release);
            return STOP;
        }
        np_single((void *)0); /* overdue: apply the first press before starting another */
    }
    st.np_press_at = now;
    st.np_press = timer_add(np_single, (void *)0, DOUBLE_CLICK_MS);
    return STOP;
}

static int np_gone(void *win, void *event) {
    (void)event;
    if (win == st.np_win) {
        st.np_win = (void *)0;
        st.np_hash = 0;
        st.np_slider = st.np_elapsed = st.np_cover = st.np_slide = st.np_lrc = st.np_control = (void *)0;
        np_cancel();
    }
    return 0;
}

/* playing_page_init: stock builds the page and starts its 250 ms timer, then the iPod labels bind.
 */
int ringnav_playing(void *win, void *ctx) {
    int result = stock_playing_trampoline(win, ctx);
    if (!win) return result;
    st.np_win = win;
    st.np_pos = widget_lookup(win, "label_ipod_pos", 1);
    st.np_album = widget_lookup(win, "label_ipod_album", 1);
    st.np_slider = widget_lookup(win, "slider_play", 1);
    st.np_remain = widget_lookup(win, "label_ipod_remain", 1);
    st.np_elapsed = widget_lookup(win, "label_playtime", 1);
    st.np_cover = widget_lookup(win, "img_cover", 1);
    st.np_slide = widget_lookup(win, "slide_view", 1);
    st.np_lrc = widget_lookup(win, "scroll_lrc", 1);
    st.np_control = widget_lookup(win, "label_ipod_control", 1);
    st.np_panel = 0;
    st.np_seek_changed = 0;
    st.np_mode = -1;
    st.np_hash = 0;
    st.np_left = -1;
    np_fill();
    np_controls();
    widget_on(win, EVT_DESTROY, np_gone, win);
    np_sync(win);
    visualizer_attach(win);
    return result;
}

/* Every VOL_POLL_MS while the dialog vol_paint drew is still on top: a changed volume repaints it
 * whole. Stock's own invalidation of the hidden slider does not reliably reach the screen, so the
 * bar would stop following the wheel after a tick or two. */
static int vol_poll(const void *info) {
    (void)info;
    void *top = window_manager_get_top_window(window_manager());
    void *vol = top && top == st.vol_dialog ? widget_lookup(top, "slider_vol", 1) : (void *)0;
    if (!vol) {
        st.vol_timer = 0;
        st.vol_dialog = (void *)0;
        return 0; /* RET_OK: removed */
    }
    if (widget_get_prop_int(vol, "value", 0) != st.vol_drawn)
        widget_invalidate_force(top, (void *)0);
    return 8; /* RET_REPEAT */
}

/* The volume, drawn by stock's dialog/volume_dialog itself: transparent and full-screen (its
 * dimming highlight removed at build time, tools/ipod.py), it opens on the wheel's volume and
 * sets its slider_vol. Over Now Playing, as on an iPod classic, the band from the progress bar to
 * the times turns black with a white bar over the track and "Volume N"; over any other window
 * (Quick Settings included) a VOL_PANEL_* rounded panel in the fast-scroll letter's style holds
 * a pill bar and the number alone. slider_vol and label_vol are hidden, and slider_vol is moved
 * onto the area drawn, so stock's partial repaints land on it; vol_poll catches the changes those
 * miss. */
static void vol_paint(void *top, void *canvas) {
    void *wm = window_manager(), *lcd = P(canvas, CANVAS_LCD);
    if (!lcd || tk_strcmp(widget_get_prop_str(top, "name", ""), "volume_dialog")) return;
    unsigned n = widget_count_children(wm);
    void *vol = widget_lookup(top, "slider_vol", 1), *label = widget_lookup(top, "label_vol", 1);
    if (n < 2 || !vol) return;
    int np = widget_get_child(wm, n - 2) == st.np_win && st.np_slider && st.np_elapsed;
    rect_t area, bar, text;
    if (np) {
        void *slider = st.np_slider, *times = st.np_elapsed;
        int dy = I(st.np_win, W_Y) - I(top, W_Y), y = dy + I(slider, W_Y),
            bh = widget_get_prop_int(slider, "bar_size", 8);
        area = (rect_t){ 0, y, I(st.np_win, W_W), dy + I(times, W_Y) + I(times, W_H) - y };
        bar = (rect_t){ I(slider, W_X), y + (I(slider, W_H) - bh) / 2, I(slider, W_W), bh };
        text = (rect_t){ 0, dy + I(times, W_Y), area.w, I(times, W_H) };
    } else {
        area = (rect_t){ VOL_PANEL_X, VOL_PANEL_Y, I(top, W_W) - 2 * VOL_PANEL_X, VOL_PANEL_H };
        int x = area.x + VOL_PANEL_PAD, bw = area.w - 2 * VOL_PANEL_PAD - VOL_PANEL_TEXT;
        bar = (rect_t){ x, area.y + (area.h - VOL_PANEL_BAR) / 2, bw, VOL_PANEL_BAR };
        text = (rect_t){ x + bw, area.y, VOL_PANEL_TEXT, area.h };
    }
    if (I(vol, W_Y) != area.y) widget_move_resize(vol, area.x, area.y, area.w, area.h);
    widget_set_visible(vol, 0, 0);
    if (label) widget_set_visible(label, 0, 0);
    int max = widget_get_prop_int(vol, "max", 100), level = widget_get_prop_int(vol, "value", 0);
    st.vol_dialog = top;
    st.vol_drawn = level;
    if (!st.vol_timer) st.vol_timer = timer_add(vol_poll, (void *)0, VOL_POLL_MS);
    unsigned fill = (unsigned)I(lcd, LCD_FILL_COLOR), s[12] = { 'V', 'o', 'l', 'u', 'm', 'e', ' ' },
             k = np ? 7 : 0;
    int w = max > 0 ? bar.w * clamp_step(0, max, level) / max : 0;
    if (np) {
        canvas_set_fill_color(canvas, RGBA(0));
        canvas_fill_rect(canvas, area.x, area.y, area.w, area.h);
    } else {
        fill_box(canvas, &area, (LETTER_ALPHA << 24) | FILL_RGB, LETTER_RADIUS);
    }
    /* Pills, like the progress capsule: a non-zero fill is at least round, never a sliver. */
    fill_box(canvas, &bar, RGBA(np ? TRACK_COLOR : VOL_PANEL_TRACK), bar.h / 2);
    if (w) {
        rect_t on = { bar.x, bar.y, w < bar.h ? bar.h : w, bar.h };
        fill_box(canvas, &on, RGBA(0xffffff), bar.h / 2);
    }
    canvas_set_fill_color(canvas, fill);
    k += put_num(s + k, (unsigned)clamp_step(0, 999, level));
    draw_centred(canvas, s, k, &text, np ? NP_TIMES_PX : VOL_PANEL_PX, 0xffffffff);
}

/* Stock paints a widget's background before its children, so the bar sits behind the rows.
 * The selection work for the surface happens here, once per frame, instead of in the border hook;
 * a BUTTONS dialog is itself top-level. Home's art is clipped to its panel until the border hook.
 * Other top-level widgets are the status bar, which gets its solid fill, and the windows. Painting
 * the top window or the bar (at least each second, systembar_showface) keeps the bar's clock,
 * Home's art and Now Playing's labels current; Home's art is also checked when Home is painted
 * under another window. */
static void settings_summary(void *win);

int ringnav_paint_bg(void *w, void *canvas) {
    settings_summary(w);
    int result = stock_paint_bg_trampoline(w, canvas);
    void *wm = window_manager(), *bar = *(void *const *)system_bar;
    paint_selection(w, canvas);
    coverflow_home_clip(w, canvas, 1);
    if (w && w == st.bar_slot) paint_battery(w, canvas);
    if (!w || P(w, W_PARENT) != wm) return result;
    if (w == bar && P(canvas, CANVAS_LCD)) {
        unsigned fill = (unsigned)I(P(canvas, CANVAS_LCD), LCD_FILL_COLOR);
        canvas_set_fill_color(canvas, RGBA(BAR_COLOR));
        canvas_fill_rect(canvas, 0, 0, I(w, W_W), I(w, W_H));
        canvas_set_fill_color(canvas, fill);
    }
    void *top = window_manager_get_top_window(wm);
    /* Any painted window, not only the top one: Home slides back in from a snapshot. */
    coverflow_home_art(w == bar ? top : w);
    if (bar && (w == bar || w == top)) {
        clock_sync(bar);
        bar_sync(bar);
        np_sync(top);
        if (w == top) vol_paint(top, canvas);
        /* Boot may paint Home before the screen is usable, so nothing chose or drew its first
         * row. Once, when the list is first reachable, repaint it. */
        void *list = st.greeted ? (void *)0 : surface((void *)0, (void *)0);
        if (list) {
            st.greeted = 1;
            widget_invalidate_force(list, (void *)0);
        }
    }
    return result;
}

/* Live accent (docs/internals.md#accent). Colors are mapped as style_get_color returns them: text
 * (text_color, highlight_text_color) to the red tone, fills and borders to the light tone. The name
 * is only compared for a color that maps. */
unsigned *ringnav_style_color(unsigned *color, void *style, const char *name, unsigned fallback) {
    stock_color_trampoline(color, style, name, fallback);
    int a = accent();
    if (a == CRIMSON) return color;
    unsigned c = accent_map(*color, a, TONE_LIGHT);
    if (c != *color && name && tk_str_end_with(name, "text_color"))
        c = accent_map(*color, a, TONE_RED);
    *color = c;
    return color;
}

/* Backgrounds come as gradients: the whole stock leaf (null style or vtable: none), then the stops
 * of the caller's gradient_t (nr @8, 8-byte {color, offset} stops @0xc, at most 8). style_get_color
 * asks here first for every color, text included, so its own call stays unmapped and the color
 * hook picks the tone. */
void *ringnav_style_gradient(void *style, const char *name, void *out) {
    void *vt = style ? P(style, 0) : (void *)0;
    void *(*get)(void *, const char *, void *) =
        vt ? (void *(*)(void *, const char *, void *))P(vt, 0x18) : (void *)0;
    void *g = get ? get(style, name, out) : (void *)0;
    int own = __builtin_return_address(0) == (void *)STYLE_COLOR_GRADIENT_RET;
    for (int i = 0; !own && g && g == out && i < I(g, 8) && i < 8; ++i)
        I(g, 0xc + 8 * i) = (int)accent_map((unsigned)I(g, 0xc + 8 * i), accent(), TONE_LIGHT);
    return g;
}

/* 1 if name is one of the settings rows' category icons (SETTINGS_ICON_NAMES in stock.h, from
 * ipod.json): their red is a category colour, like the purple and orange ones, not an accent. */
static int settings_icon(const char *name) {
    for (const char *n = SETTINGS_ICON_NAMES; *n;) {
        if (!tk_strcmp(name, n)) return 1;
        while (*n++) {}
    }
    return 0;
}

/* Decoded theme images are mapped once, before the image manager caches them. Only a plain asset
 * name is the theme's: covers by path or URL (a '/' or ':') never are, nor are the settings icons.
 * The confirm pop-up's discs (CONFIRM_IMAGE*) take the dark CONFIRM_SURFACE under every accent,
 * Crimson included, so their white glyphs stay legible. The quick settings' active controls
 * (DROPDOWN_IMAGE*, not the brightness suns) take the accent's red tone like everything else red,
 * but their white glyph turns CONFIRM_SURFACE on a tone brighter than GLYPH_LIGHT_MAX (Graphite's
 * silver), so an active disc stands apart from the grey inactive ones and its glyph stays legible.
 * Any other red under white (a switch's knob, a disc's glyph, a BUTTON_IMAGE*'s label) is a surface
 * and takes the light tone, which keeps the white legible; red marks on their own keep the red
 * tone. bitmap_t: w @0, h @4, format @0xe; the 32-bit formats 1-4 hold r, g, b at these byte
 * offsets. */
int ringnav_image_add(void *manager, const char *name, void *bitmap) {
    static const unsigned char at[4][4] = BITMAP_RGBA_AT;
    unsigned format = bitmap ? *(unsigned short *)((char *)bitmap + 0xe) - 1u : 4,
             preset = accent();
    unsigned char *data = (void *)0;
    const char *s = name;
    int dark = s && tk_str_start_with(s, CONFIRM_IMAGE);
    int control = s && tk_str_start_with(s, DROPDOWN_IMAGE) && !tk_str_start_with(s, DROPDOWN_SUN);
    unsigned tone = dark ? CONFIRM_SURFACE : accents[preset][TONE_RED], glyph = 0xffffff,
             light = accents[preset][TONE_LIGHT];
    if (control && ((tone >> 16) * 299 + (tone >> 8 & 255) * 587 + (tone & 255) * 114) / 1000 >
                       GLYPH_LIGHT_MAX)
        glyph = CONFIRM_SURFACE;
    while (s && *s && *s != '/' && *s != ':') ++s;
    if ((preset != CRIMSON || dark) && format < 4 && s && !*s && !settings_icon(name))
        data = bitmap_lock_buffer_for_write(bitmap);
    if (data) {
        const unsigned char *o = at[format];
        unsigned stride = bitmap_get_line_length(bitmap);
        /* Only an active control holds red; an inactive one keeps its white glyph. */
        int red = 0, white = tk_str_start_with(name, BUTTON_IMAGE);
        int scan = control ? glyph != 0xffffff : !dark && light != tone;
        for (int y = 0; scan && !(red && white) && y < I(bitmap, 4); ++y)
            for (unsigned char *p = data + y * stride, *end = p + 4 * I(bitmap, 0); p < end;
                 p += 4) {
                unsigned c = p[o[0]] | p[o[1]] << 8 | p[o[2]] << 16;
                if (red_map(c, tone, 0xffffff) != c)
                    red = 1;
                else if ((c == 0xffffff || c == 0x7f7f7f) &&
                         p[o[3]] == 255) /* or a pressed disc's */
                    white = 1;
            }
        if (!red)
            glyph = 0xffffff;
        else if (!control && white)
            tone = light;
        for (int y = 0; y < I(bitmap, 4); ++y)
            for (unsigned char *p = data + y * stride, *end = p + 4 * I(bitmap, 0); p < end;
                 p += 4) {
                unsigned c = red_map(p[o[0]] | p[o[1]] << 8 | p[o[2]] << 16, tone, glyph);
                p[o[0]] = c;
                p[o[1]] = c >> 8;
                p[o[2]] = c >> 16;
            }
        bitmap_unlock_buffer(bitmap);
    }
    return stock_image_trampoline(manager, name, bitmap);
}

static void setting_text(int i) {
    static const char *const home[] = { "Home: Split", "Home: Full" }, *const battery[] = {
        "Battery: Icon", "Battery: Percent", "Battery: Icon + Percent"
    };
    const char *const names[] = { accent_names[accent()], home[st.home_full], battery[st.battery] };
    setting_pair_text(st.setting_label[i], names[i]);
}

/* Centre or tap cycles the row's value and saves it. A new accent reaches the payload's drawing on
 * the next paint, and the theme's colors and images once every cached image is dropped and the
 * screen repaints; Home takes its new layout at once, as it is never recreated. */
static int setting_click(void *ctx, void *event) {
    (void)event;
    static const char *const keys[] = { "ACCENT", "HOME", "BATTERY" };
    static const int counts[] = { ACCENT_N, 2, 3 };
    int i = (int)(long)ctx; /* read by ringnav_display: 0 Accent, 1 Home, 2 Battery */
    int *const values[] = { &st.accent, &st.home_full, &st.battery }, *value = values[i];
    *value = (*value + 1) % counts[i];
    write_int_config(*value, "IPOD", keys[i]);
    if (i == 2)
        widget_invalidate_force(*(void *const *)system_bar, (void *)0); /* bar_sync applies it */
    else if (i == 1)
        coverflow_home_layout();
    else if (!i) {
        np_fill();
        image_manager_unload_all(image_manager());
        widget_invalidate_force(window_manager(), (void *)0);
    }
    setting_text(i);
    return 0;
}

/* systemset_display_page_init: stock builds its three rows (0x4c19bc: a s_listitem_black list_item
 * holding a 335x70 s_btn_listitem button with a 52px icon, a 24px label at x 72 and list_into); the
 * Accent, Home and Battery rows follow with the same widgets and styles, borrowing the Display,
 * cover mode and power manager icons, the value in the label and no chevron, since they change in
 * place. */
int ringnav_display(void *win, void *ctx) {
    int result = stock_display_trampoline(win, ctx);
    void *view = win ? widget_lookup(win, "scroll_view_display", 1) : (void *)0;
    static const char *const icons[] = { "system_display", "playset_covermode",
                                         "system_powermanager" };
    for (int i = 0; view && i < 3; ++i) {
        st.setting_label[i] = list_row(view, icons[i], setting_click, (void *)(long)i);
        setting_text(i);
    }
    return result;
}

/* Stock retains the prompt, optional delete-source checkbox and result callbacks. Only exact
 * translated prompt matches name the action; unknown confirmations keep a neutral Continue. */
int ringnav_confirm_dialog(void *win, void *ctx) {
    int result = stock_confirm_dialog_trampoline(win, ctx);
    if (!win || !ctx || result) return result;
    static const struct { const char *key, *action; int accept; } actions[] = {
        { "msg_delplaylist", "Delete playlist", 0 }, { "msg_delsong", "Delete song", 0 },
        { "msg_delfile", "Delete file", 0 }, { "msg_delalbum", "Delete album", 0 },
        { "msg_delartist", "Delete artist", 0 }, { "msg_delgenre", "Delete genre", 0 },
        { "msg_delete_task", "Delete download", 0 }, { "msg_poweroff", "Shut down", 0 },
        { "msg_confirmupdate", "Install update", 0 }, { "msg_wificancelsave", "Forget network", 0 },
        { "msg_wifidisconnect", "Disconnect", 0 }, { "msg_unpairing", "Unpair device", 0 },
        { "msg_exitairplay", "Quit AirPlay", 0 }, { "msg_lowarn", "Enable line out", 0 },
        { "msg_exportplaylist", "Export playlist", 0 }, { "msg_importplaylist", "Import playlist", 0 },
        { "msg_actionscan", "Scan music", 1 }, { "msg_btreconnect", "Reconnect", 1 },
    };
    const char *prompt = (const char *)ctx + 8, *action = "Continue";
    int accept = 0, scan = 0;
    for (unsigned i = 0; i < sizeof actions / sizeof *actions; ++i) {
        const char *translated = locale_info_tr(locale_info(), actions[i].key);
        int is_scan = !tk_strcmp(actions[i].key, "msg_actionscan");
        int matched = !tk_strcmp(prompt, actions[i].key) ||
                      (translated && !tk_strcmp(prompt, translated));
        /* Startup's type-2 prompt puts the card notice first and the scan question second. */
        if (!matched && is_scan && B(ctx, 0) == 2) {
            const char *question = (const char *)ctx + 0x208;
            matched = !tk_strcmp(question, actions[i].key) ||
                      (translated && !tk_strcmp(question, translated));
        }
        if (matched) {
            action = actions[i].action;
            accept = actions[i].accept;
            scan = is_scan;
            break;
        }
    }
    widget_set_text_utf8(widget_lookup(win, "img_cancel", 1), "Cancel");
    widget_set_text_utf8(widget_lookup(win, "img_enter", 1), action);
    prop(win, "_selection_default", accept);
    prop(win, "_scan_after_usb", scan);
    return result;
}
#else
#define np_cancel() ((void)0)
#endif

#if IPOD
static int untouch(const void *info) {
    (void)info;
    st.untouch_timer = 0;
    st.touch_mode = 0;
    void *top = window_manager_get_top_window(window_manager());
    if (top) widget_invalidate_force(top, (void *)0);
    return 0;
}
#endif

static void hide_outline(void) {
    stop_timer(&st.untouch_timer);
    st.touch_mode = 1;
    void *top = window_manager_get_top_window(window_manager());
    if (top) widget_invalidate_force(top, (void *)0);
}

int ringnav_touch(void *ctx, void *event) {
    np_cancel(); /* before the slider sees the touch, so a drag seeks the stock way */
    hide_outline();
    int result = stock_touch_trampoline(ctx, event);
    /* A tap is a fresh interaction: it cancels a pending screen-toggle pair and any spin. */
    drop_input();
    fx_cancel();
    /* Stock move-before forwards to this down-before entry too. Only a real down starts
     * a new pull; the page's native before-children callbacks observe subsequent motion. */
    if (!event || I(event, EVENT_TYPE) == EVT_POINTER_DOWN) pull_begin(event);
    void *w = surface((void *)0, (void *)0);
    /* Pointer-down must not recall/rebind the row that native touch is about to hit. */
    if (!result && w && load_rows(&g_menu, w)) {
        stop_scroll(&g_menu);
        prop(w, TOUCH, 1);
        widget_invalidate_force(w, (void *)0);
    }
    return result; /* The very same touch continues through the stock tap/drag handlers. */
}

/* Nearest collected ancestor of a tap: a clickable child selects the row that owns it. */
static int selects(menu_t *m, void *target) {
    for (int depth = 0; target && depth < 32; ++depth) {
        for (int i = 0; i < m->n; ++i)
            if (m->at[i] == target) return i;
        if (target == m->w) break;
        target = P(target, W_PARENT);
    }
    return -1;
}

/* Observe actual clicks BEFORE app callbacks can navigate or destroy/rebind their widgets.
 * Do not turn pointer-down into selection: a swipe is not a tap. */
#if IPOD
static int bluetooth_device(void *button) {
    for (unsigned i = 0; button && i < widget_count_children(button); ++i) {
        void *c = widget_get_child(button, i);
        if (!tk_strcmp(widget_get_type(c), "image") &&
            !tk_strcmp(widget_get_prop_str(c, "image", ""), "bt_lefticon")) return 1;
    }
    return 0;
}
#endif

int ringnav_dispatch(void *target, void *event) {
    if (target && event && I(event, EVENT_TYPE) == EVT_POINTER_DOWN) {
        void *top = window_manager_get_top_window(window_manager());
        int count = field_count(top);
        void *w = target;
        for (int depth = 0; count && w && w != top && depth < 32; ++depth, w = P(w, W_PARENT)) {
            for (int i = 0; i < count; ++i) {
                if (w != field_target(top, i) || !control_available(w, top)) continue;
                stop_timer(&st.editor_timer);
                st.editor_page = (void *)0;
                prop(top, "_wheel_field", i);
                widget_invalidate_force(top, (void *)0);
                break;
            }
        }
    }
#if IPOD
    if (st.pull_page && (!event || !pull_live() || I(event, EVENT_TYPE) == EVT_KEY_DOWN_BEFORE))
        pull_cancel();
    if (target && event && I(event, EVENT_TYPE) == EVT_CLICK) {
        for (void *w = target; w; w = P(w, W_PARENT))
            if (widget_get_prop_int(w, PULL_SUPPRESS, 0)) return STOP;
        pull_cancel();
    }
#endif
    if (target && event && I(event, EVENT_TYPE) == EVT_CLICK) {
        hide_outline();
        cancel_center(); /* A native activation supersedes confirmation, even without touch. */
        stop_timer(&st.editor_timer);
        st.editor_page = (void *)0;
        drop_spin();
        fx_cancel();
        void *other = (void *)0;
        void *w = surface(target, &other);
        /* A tap owns its live row: recall could scroll/rebind that row before delivery. */
        if (w && load(&g_menu, w, 0)) {
            int i = selects(&g_menu, target);
            if (i >= 0) {
                stop_scroll(&g_menu); /* an explicit tap replaces any pending recall glide */
                if (other) {
                    prop(other, SEL, -1);
                    widget_invalidate_force(other, (void *)0);
                }
                select(&g_menu, g_menu.id[i]);
                widget_invalidate_force(w, (void *)0);
            }
        }
    }
#if IPOD
    /* Stock interprets clicks at x >= 276 as unpair. Device rows now only connect. */
    if (target && event && I(event, EVENT_TYPE) == EVT_CLICK) {
        void *owner = window_of(target);
        if (owner && !tk_strcmp(widget_get_prop_str(owner, "name", ""), "bluetooth_page")) {
            void *button = target;
            while (button && button != owner && !bluetooth_device(button)) button = P(button, W_PARENT);
            if (button && button != owner) {
                char click[0x30];
                return stock_dispatch_trampoline(button, pointer_event_init(click, EVT_CLICK, button, 0, 0));
            }
        }
    }
#endif
    return stock_dispatch_trampoline(target, event);
}

#if IPOD

/* Only audited ordinary row constructors install this per-instance layouter. Stock still
 * positions every child, skips hidden controls, and owns clone/destruction and text overflow. */
static int compact_row_layout(void *layout, void *row) {
    void *text = (void *)0, *title = (void *)0;
    unsigned count = widget_count_children(row);
    /* Names differ (view_info, viewinfo_N, ...); all seven constructors put the title
     * first, either directly in the row or one level inside its text container. */
    for (unsigned i = 0; i < count && !text; ++i) {
        void *child = widget_get_child(row, i);
        if (!tk_strcmp(widget_get_type(child), "hscroll_label")) text = title = child;
        for (unsigned j = 0; j < widget_count_children(child) && !text; ++j) {
            void *label = widget_get_child(child, j);
            if (!tk_strcmp(widget_get_type(label), "hscroll_label")) {
                text = child;
                title = label;
            }
        }
    }
    if (text && widget_get_visible(text)) {
        int width = I(row, W_W) - 2 * B(layout, DEFAULT_LAYOUT_X_MARGIN);
        if (drill(row)) /* as if a stock img_into were the last child */
            width -=
                CHEVRON_W - B(layout, DEFAULT_LAYOUT_X_MARGIN) + B(layout, DEFAULT_LAYOUT_SPACING);
        for (unsigned i = 0; i < count; ++i) {
            void *child = widget_get_child(row, i);
            if (child != text && widget_get_visible(child))
                width -= I(child, W_W) + B(layout, DEFAULT_LAYOUT_SPACING);
        }
        if (width < 0) width = 0;
        widget_resize(text, width, I(text, W_H));
        if (title != text) {
            int available = width - I(title, W_X);
            widget_resize(title, available > 0 ? available : 0, I(title, W_H));
        }
    }
    return ((int (*)(void *, void *))((const unsigned *)DEFAULT_LAYOUT_VTABLE)[2])(layout, row);
}

int compact_set_row_layout(void *row, const char *params) {
    static unsigned vtable[8] __attribute__((section(".scratch")));
    int ret = widget_set_children_layout(row, params);
    if (ret || !row) return ret;
    void *layout = P(row, W_CHILDREN_LAYOUT);
    if (layout) {
        if (!vtable[0]) {
            memcpy(vtable, (const void *)DEFAULT_LAYOUT_VTABLE, sizeof(vtable));
            vtable[2] = (unsigned)compact_row_layout;
        }
        P(layout, CHILDREN_LAYOUT_VTABLE) = vtable;
    }
    return ret;
}

/* Settings rows (docs/ipod.md#settings). One stock child in row coordinates: full-height children
 * fill the row and shorter ones keep their centre; the icon shrinks to SET_ICON; text starts at
 * the corner-safe column; children from SET_RIGHT_SIDE, and wide ones' right edges, move with the
 * trailing image by `shift`, never past the text margin. */
static void set_child(void *c, int row_w, int row_h, int shift) {
    int x = I(c, W_X), y = I(c, W_Y), w = I(c, W_W), h = I(c, W_H), right = x + w;
    if (h >= SET_STOCK_BODY) {
        y = 0;
        h = row_h;
    } else
        y -= (SET_STOCK_BODY - row_h) / 2;
    if (w == SET_STOCK_ICON && !tk_strcmp(widget_get_type(c), "image")) {
        /* the build pre-sizes the audited icons to SET_ICON, so they draw 1:1; any other one scales
         * down */
        image_set_draw_type(c, IMAGE_DRAW_SCALE_DOWN);
        widget_move_resize(c, SET_ICON_X, (row_h - SET_ICON) / 2, SET_ICON, SET_ICON);
        return;
    }
    int left = x >= SET_RIGHT_SIDE ? SET_STOCK_X + x + shift
               : x > SET_STOCK_ICON / 2 + 10 /* after a stock icon at x 10 */
                   ? SET_ICON_X + SET_ICON + SET_GAP + x - (SET_STOCK_ICON + 20)
                   : SET_TEXT_X + x - 10;
    right = right >= SET_RIGHT_SIDE ? SET_STOCK_X + right + shift : right + left - x;
    if (right > row_w - SET_TEXT_X) right = row_w - SET_TEXT_X;
    widget_move_resize(c, left, y, right > left ? right - left : 0, h);
}

/* The row's stock settings button (SET_STOCK_X, SET_STOCK_W), or none: a row already mapped, or one
 * another builder made, which keeps its own height and geometry. */
static void *set_button(void *item) {
    if (tk_strcmp(widget_get_type(item), "list_item")) return (void *)0;
    for (unsigned i = 0; i < widget_count_children(item); ++i) {
        void *b = widget_get_child(item, i);
        if (I(b, W_X) == SET_STOCK_X && I(b, W_W) == SET_STOCK_W &&
            !tk_strcmp(widget_get_type(b), "button"))
            return b;
    }
    return (void *)0;
}

/* The stock button spans the row; its children follow. Once mapped it no longer matches, so a later
 * layout leaves it alone; the builders give its children no self_layout, so nothing lays them out
 * again. */
static void set_row(void *item) {
    int row_w = I(item, W_W), row_h = I(item, W_H);
    void *b = set_button(item);
    if (b) {
        int trail = SET_STOCK_TRAIL; /* the rightmost trailing image sets the right column */
        unsigned n = widget_count_children(b);
        for (unsigned j = 0; j < n; ++j) {
            void *c = widget_get_child(b, j);
            if (I(c, W_W) == 50 && I(c, W_X) >= SET_RIGHT_SIDE && I(c, W_X) > trail &&
                !tk_strcmp(widget_get_type(c), "image"))
                trail = I(c, W_X);
        }
        widget_move_resize(b, 0, 0, row_w, row_h);
        for (unsigned j = 0; j < n; ++j)
            set_child(widget_get_child(b, j), row_w, row_h,
                      row_w - SET_EDGE - 50 - SET_STOCK_X - trail);
        void *labels[2];
        unsigned found = 0;
        for (unsigned j = 0; j < n; ++j) {
            void *c = widget_get_child(b, j);
            const char *type = widget_get_type(c);
            if (!tk_strcmp(type, "label") || !tk_strcmp(type, "hscroll_label")) {
                if (found < 2) labels[found] = c;
                ++found;
            }
        }
        if (bluetooth_device(b)) {
            for (unsigned j = 0; j < n; ++j) {
                void *c = widget_get_child(b, j);
                const char *image = widget_get_prop_str(c, "image", "");
                if (!tk_strcmp(image, "navbar_delete")) widget_set_visible(c, 0, 0);
                if (!tk_strcmp(image, "bt_lefticon")) {
                    image_set_draw_type(c, IMAGE_DRAW_SCALE_DOWN);
                    widget_move_resize(c, SET_ICON_X, (row_h - SET_ICON) / 2, SET_ICON, SET_ICON);
                }
            }
            for (unsigned j = 0; j < found && j < 2; ++j)
                widget_move_resize(labels[j], SET_ICON_X + SET_ICON + SET_GAP, found == 2 ? (j ? 40 : 12) : 24,
                                   row_w - SET_TEXT_X - SET_ICON_X - SET_ICON - SET_GAP, j ? 16 : 20);
        } else if (found == 2 && I(labels[0], W_H) == row_h && I(labels[1], W_H) == row_h)
            setting_pair_layout(labels[0], labels[1]);
    }
}

/* The list_view layouter's vtable slot. A list whose asset default_item_height is SET_ROW (only
 * the iPod settings pages) gets settings rows: items stock made SET_STOCK_ROW high take SET_ROW
 * before the stock layout stacks them and sizes the scroll view, then each row is mapped. Settings
 * code never moves, resizes or scrolls its rows afterwards (docs/ipod.md#settings). */
static void bluetooth_remove_option(void *view);

int ipod_list_layout(void *layout, void *view) {
    bluetooth_remove_option(view);
    void *list = view ? P(view, W_PARENT) : (void *)0;
    int rows = list && !tk_strcmp(widget_get_type(list), "list_view") && !I(list, ROW_HEIGHT) &&
               I(list, LIST_DEFAULT_ITEM_HEIGHT) == SET_ROW;
    unsigned n = rows ? widget_count_children(view) : 0;
    for (unsigned i = 0; i < n; ++i) {
        void *item = widget_get_child(view, i);
        if (I(item, W_H) == SET_STOCK_ROW && set_button(item)) I(item, W_H) = SET_ROW;
    }
    int ret = ((int (*)(void *, void *))LIST_VIEW_LAYOUT)(layout, view);
    for (unsigned i = 0; i < n; ++i) set_row(widget_get_child(view, i));
    return ret;
}

/* Resolve current stock rows by device address. Scans can rebuild them while this screen is open. */
static void *bluetooth_view(void) {
    void *owner = widget_lookup(window_manager(), "bluetooth_page", 0);
    return owner ? widget_lookup(owner, "scroll_view_bluetooth", 1) : (void *)0;
}

static const char *bluetooth_address(void *button) {
    const char *name = widget_get_prop_str(button, "name", "");
    if (!*name) return (void *)0;
    unsigned index = 0;
    for (const char *s = name; *s; ++s) {
        if (*s < '0' || *s > '9') return (void *)0;
        index = index * 10 + *s - '0';
    }
    void *devices = P(pdeq_btshowlist, 0);
    return devices && index < deque_size(devices) ? (const char *)deque_at(devices, index) + 4 : (void *)0;
}

static int bluetooth_paired(void *button) {
    if (!bluetooth_device(button)) return 0;
    for (unsigned i = 0; i < widget_count_children(button); ++i)
        if (!tk_strcmp(widget_get_prop_str(widget_get_child(button, i), "image", ""), "navbar_delete")) return 1;
    return 0;
}

static void bluetooth_remove_refresh(void *page);

static int bluetooth_remove_pick(void *item, void *event) {
    (void)event;
    void *page = window_of(item), *view = bluetooth_view();
    const char *address = widget_get_prop_str(item, "_bt_address", "");
    for (unsigned i = 0; view && i < widget_count_children(view); ++i) {
        void *button = widget_get_child(widget_get_child(view, i), 0);
        const char *current = bluetooth_paired(button) ? bluetooth_address(button) : (void *)0;
        if (current && !tk_strcmp(address, current)) {
            char click[0x30];
            /* Stock retains its confirmation, busy checks, disconnect and persistence. */
            stock_dispatch_trampoline(button, pointer_event_init(click, EVT_CLICK, button, 300, 0));
            break;
        }
    }
    bluetooth_remove_refresh(page);
    return 0;
}

static int bluetooth_remove_closed(void *ctx, void *event) { (void)ctx; (void)event; return 0; }
static int bluetooth_remove_key(void *ctx, void *event) {
    (void)ctx;
    if (I(event, EVENT_KEY) != KEY_RETURN) return 0;
    window_close(window_manager_get_top_window(window_manager()));
    return STOP;
}

static void bluetooth_remove_refresh(void *page) {
    void *source = bluetooth_view();
    unsigned count = 0;
    for (unsigned i = 0; source && i < widget_count_children(source); ++i)
        count += bluetooth_paired(widget_get_child(widget_get_child(source, i), 0));
    void *view = page_list(page, page, 0, count ? "Remove devices" : "No paired devices", count, 48);
    unsigned row = 0;
    for (unsigned i = 0; source && i < widget_count_children(source); ++i) {
        void *button = widget_get_child(widget_get_child(source, i), 0);
        const char *address = bluetooth_paired(button) ? bluetooth_address(button) : (void *)0;
        if (!address) continue;
        void *item = list_item_create(view, 0, row++ * 48, 375, 48);
        widget_use_style(item, "s_listitem_black");
        void *label = hscroll_label_create(item, 30, 0, 285, 48);
        widget_use_style(label, "s_scrlabel_white20l");
        set_hscroll_label_attribute(label);
        for (unsigned j = 0; j < widget_count_children(button); ++j) {
            void *c = widget_get_child(button, j);
            if (!tk_strcmp(widget_get_type(c), "hscroll_label")) {
                widget_set_text(label, widget_get_text(c));
                break;
            }
        }
        widget_set_prop_str(item, "_bt_address", address);
        widget_on(item, EVT_CLICK, bluetooth_remove_pick, item);
    }
}

static int bluetooth_remove_open(void *ctx, void *event) {
    (void)ctx; (void)event;
    if (widget_lookup(window_manager(), "btremove_page", 0)) return 0;
    void *page = page_open("btremove_page", bluetooth_remove_closed, bluetooth_remove_key);
    if (page) bluetooth_remove_refresh(page);
    return 0;
}

static void bluetooth_remove_option(void *view) {
    if (!view || tk_strcmp(widget_get_prop_str(view, "name", ""), "scroll_view_bluetooth")) return;
    if (widget_lookup(view, "bt_remove_devices", 1)) return;
    void *label = list_row(view, (void *)0, bluetooth_remove_open, (void *)0);
    widget_set_name(P(label, W_PARENT), "bt_remove_devices");
    widget_set_text_utf8(label, "Remove devices");
}

/* Menu summaries use the live values, so returning from an editor never shows the old setting. */
static void settings_summary(void *win) {
    if (!win) return;
    const char *name = widget_get_prop_str(win, "name", "");
    const char *view_name = !tk_strcmp(name, "display_page") ? "scroll_view_display"
                            : !tk_strcmp(name, "playset_page") ? "scroll_view_playset" : (void *)0;
    if (!view_name) return;
    void *view = widget_lookup(win, view_name, 1);
    for (unsigned i = 0; view && i < widget_count_children(view); ++i) {
        void *row = widget_get_child(view, i);
        void *b = widget_get_child(row, 0), *title = (void *)0, *value = (void *)0;
        const char *icon = "";
        for (unsigned j = 0; b && j < widget_count_children(b); ++j) {
            void *c = widget_get_child(b, j);
            const char *type = widget_get_type(c);
            if (!tk_strcmp(type, "image") && I(c, W_X) < 100)
                icon = widget_get_prop_str(c, "image", "");
            if (!tk_strcmp(type, "hscroll_label") || !tk_strcmp(type, "label")) {
                if (!title) title = c;
                else value = c;
            }
        }
        char s[32];
        const char *caption = (void *)0;
        int current = 0;
        if (!tk_strcmp(icon, "display_backlight")) {
            caption = "Brightness";
            current = g_lightness;
            tk_snprintf(s, sizeof s, "%u%%", g_lightness);
        } else if (!tk_strcmp(icon, "playset_maxvol")) {
            caption = "Max volume";
            current = g_maxvolume;
            tk_snprintf(s, sizeof s, "%u", g_maxvolume);
        } else if (!tk_strcmp(icon, "playset_bootvol")) {
            caption = "Startup volume";
            current = g_bootvolume | (g_bootvol_flag << 8);
            tk_snprintf(s, sizeof s, g_bootvol_flag ? "%u" : "Last used", g_bootvolume);
        } else if (!tk_strcmp(icon, "playset_balance")) {
            caption = "Balance";
            int balance = mclGetBalance();
            current = balance;
            tk_snprintf(s, sizeof s, !balance ? "Center" : balance < 0 ? "Left %d" : "Right %d",
                        balance < 0 ? -balance : balance);
        }
        if (caption && title) {
            if (widget_get_prop_int(title, "_summary_ready", 0) &&
                widget_get_prop_int(title, "_summary_value", 0) == current) continue;
            if (value) prop(title, "_setting_value", (int)value);
            setting_pair(title, caption, s);
            prop(title, "_summary_ready", 1);
            prop(title, "_summary_value", current);
        }
    }
}

/* Called only by stock long Return, after its power/lock gates and release guard. */
int compact_now_playing(void) {
    pull_cancel();
    drop_input();
    void *wm = window_manager();
    void *top = window_manager_get_top_window(wm);
    fx_cancel();
    if (usable() && top && !window_manager_is_animating(wm)) {
        if (!tk_strcmp(widget_get_prop_str(top, "name", ""), "playing_page"))
            navigator_back_to_home();
        else {
            static const int context[4] = { 0, 0, 0xff, 2 };
            navigator_switch_to_with_context("playing_page", context, 0);
            /* The window switch stops the hold's key-up from reaching the stock release
             * filter, so the latch it armed would otherwise swallow the next Return. Drop it
             * once the switch really landed, so the first short Return goes back. */
            void *now = window_manager_get_top_window(wm);
            if (now && !tk_strcmp(widget_get_prop_str(now, "name", ""), "playing_page"))
                *(volatile unsigned char *)RETURN_RELEASE_LATCH = 0;
        }
    }
    return 0;
}

#endif

/* Restore the remembered queue without autoplay, including In-Vehicle mode. Mode 3 makes
 * player_start pause after loading; Play/Pause starts it when the user is ready. */
void ringnav_boot(const char *page, const int *ctx) {
    (void)page;
    if (ctx[2] != 0xff) player_start((void *)ctx[0], ctx[1], ctx[2], 3);
}

/* Play/Pause hold opens Now Playing without making the same gesture a Home toggle. */
static void open_now_playing(void) {
    pull_cancel();
    drop_input();
    void *wm = window_manager();
    void *top = window_manager_get_top_window(wm);
    fx_cancel();
    if (usable() && top && !window_manager_is_animating(wm) &&
        tk_strcmp(widget_get_prop_str(top, "name", ""), "playing_page")) {
        static const int context[4] = { 0, 0, 0xff, 2 };
        navigator_switch_to_with_context("playing_page", context, 0);
    }
}

/* Centre hold queue menu (docs/internals.md). Stock long press fires once per press, so a hold
 * on a local song, album, artist/composer/genre or folder row opens the stock sortselect dialog
 * rebuilt as that row's menu. */
enum { QM_SONG = 1, QM_ALBUM, QM_GROUP, QM_FOLDER, QM_COVERFLOW, QM_COVERALBUM };
enum { QA_NEXT = 1, QA_ADD, QA_SHUFFLE, QA_FAV, QA_UNFAV, QA_PLAYLIST, QA_ALBUM, QA_ARTIST };
#define MCL(a) (*(volatile int *)(a))

static char *input_key(unsigned key) {
    char *s = window_manager_get_input_device_status(window_manager());
    for (int i = 0; s && i < INPUT_KEY_COUNT; ++i)
        if ((unsigned)I(s, INPUT_KEYS + i * INPUT_KEY_SIZE) == key)
            return s + INPUT_KEYS + i * INPUT_KEY_SIZE;
    return (char *)0;
}

/* Any release of the held key ends the latch; only that held press is swallowed. */
static int hold_released(unsigned key) {
    char *k = input_key(key);
    int held = k && st.hold_press && *(unsigned long long *)(k + INPUT_KEY_TIME) == st.hold_press;
    st.hold_press = 0;
    return held;
}

static unsigned rec_hash(void *r) {
    unsigned h = FNV_SEED;
    for (int o = REC_NAME; o <= REC_ARTIST; o += 4) h = fnv(h, P(r, o));
    return h;
}

/* Everything a row's tracks are resolved from; a change while the menu is open cancels it. */
static unsigned browse_hash(void) { return local_hash(hash_bytes(FNV_SEED, g_folder_path, 1024)); }

/* Coverflow's or Most Played's tracks, when page is theirs. */
static void *page_tracks(void *page) {
    return page && page == st.mp_page ? st.mp_list : coverflow_tracks(page);
}

static void *qm_list(void) {
    return st.qm_kind == QM_COVERFLOW ? page_tracks(window_manager_get_top_window(window_manager()))
                                      : P(p_deque_showlist, 0);
}

static void *qm_record(void) {
    if (st.qm_kind == QM_COVERALBUM) {
        void *r = coverflow_album(window_manager_get_top_window(window_manager()));
        return r && rec_hash(r) == st.qm_hash ? r : 0;
    }
    void *list = qm_list();
    if (!list || deque_size(list) != st.qm_rows || st.qm_idx >= st.qm_rows ||
        (st.qm_kind != QM_COVERFLOW && browse_hash() != st.qm_browse))
        return (void *)0;
    void *r = deque_at(list, st.qm_idx);
    return r && rec_hash(r) == st.qm_hash ? r : (void *)0;
}

static void qm_close(void) {
    void *dialog = st.qm_dialog;
    st.qm_dialog = (void *)0;
    if (dialog) window_close(dialog);
}

/* Return and the title bar arrow dismiss without the stock sort-change flag. */
static int qm_back(void *dialog, void *event) {
    (void)dialog;
    if (I(event, EVENT_TYPE) == EVT_KEY_UP && I(event, EVENT_KEY) != KEY_RETURN) return 0;
    qm_close();
    return STOP;
}

static int qm_gone(void *dialog, void *event) {
    (void)event;
    if (st.qm_dialog == dialog) st.qm_dialog = (void *)0;
    return 0;
}

/* An album, artist/composer/genre or folder row: the query stock would run for it, as
 * batch_add_file (0x4f4be8) expands each class. Artist 0xf004/0xff01 rows name the artist at +0x18,
 * composer 0xf005/0xff02 at +0x20, genre 0xf006/0xff03 at +0x1c; classinfo +0xa.. flags Unknown. */
static const unsigned char qm_by[] = { REC_ARTIST, 0x20, 0x1c };
static int class_query(unsigned cls, void *r) {
    unsigned g = (cls & 0xf) - (cls < 0xff00 ? 4 : 1);
    int artist = I(g_artist_type, 0) == 1, id = I(r, REC_ID);
    if (cls == CLASS_ALBUMS) return getMusicByAlbum(id == -1 ? (const char *)0 : P(r, REC_ALBUM));
    if (cls < 0xff00)
        return (g == 2   ? getMusicByGenre
                : g      ? getMusicByComposer
                : artist ? getMusicByAlbumArtist
                         : getMusicBySonger)(id == -1 ? (const char *)0 : P(r, qm_by[g]));
    /* load_album_detaillist 0xff01 -> load_localclass_list 0xff11, and its composer/genre kin */
    return (g == 2   ? getMusicByAlbumAndGenre
            : g      ? getMusicByAlbumAndComposer
            : artist ? getMusicByAlbumAndAlbumSonger
                     : getMusicByAlbumAndSonger)(
        P(r, REC_ALBUM), g_local_classinfo_save[0xa + g] ? (const char *)0 : P(r, qm_by[g]),
        id == -2);
}

static int qm_query(void *r) {
    if (st.qm_kind == QM_FOLDER) {
        char path[1024];
        tk_snprintf(path, sizeof(path), "%s/%s", (const char *)g_folder_path,
                    (const char *)P(r, REC_NAME));
        return toolsLoadDirectory(path);
    }
    return class_query(st.qm_cls, r);
}

/* load_localclass_list: stock fills p_deque_showlist and returns its size. getAllAlbum,
 * getAllArtist (and its album-artist twin), getAllComposer and getAllGenre end with an Unknown row
 * (id -1) whenever the library has songs; it goes when the query its press runs finds none. Stock
 * clears the staging deque before returning, and so does this. Stock never sets g_artist_type,
 * which picks the album-artist queries for Artists and an artist's albums, so those lists always
 * went by the Artist tag; it follows artist_type, the setting the artist page itself uses. */
int ringnav_localclass(int cls) {
    I(g_artist_type, 0) = I(artist_type, 0) == 1;
    int n = stock_localclass_trampoline(cls);
    void *list = P(p_deque_showlist, 0), *dir = P(tools_pdeq_directory, 0);
    if (n <= 0 || cls < CLASS_ALBUMS || cls > 0xf006 ||
        I(deque_at(list, (unsigned)n - 1), REC_ID) != -1)
        return n;
    class_query((unsigned)cls, deque_at(list, (unsigned)n - 1));
    int empty = !deque_size(dir);
    deque_clear(dir);
    if (empty) deque_pop_back(list);
    return n - empty;
}

/* The tracks stock would play for that row, in its order. */
static void qm_tracks(void *r, void *add) {
    int n;
    void *rows = staged(qm_query, r, &n);
    for (unsigned i = 0; i < deque_size(rows); ++i) {
        void *t = deque_at(rows, i);
        if (I(t, REC_TYPE) == 8) _deque_push_back(add, t);
    }
    deque_destroy(rows);
}

/* The row's tracks: the song itself, or what stock would play for the row. */
static void *qm_collect(void *r) {
    if (st.qm_kind == QM_COVERALBUM) return coverflow_album_tracks(r);
    void *add = _create_deque("stSongInfo");
    deque_init(add);
    if (st.qm_kind == QM_SONG || st.qm_kind == QM_COVERFLOW)
        _deque_push_back(add, r);
    else
        qm_tracks(r, add);
    return add;
}

/* Insert at pos+1 or append, then keep the shuffle pool, previous index and gapless preload
 * consistent with the shifted indices. Playback state itself is never touched. */
static void qm_insert(void *queue, void *add, unsigned size, int next) {
    unsigned n = deque_size(add), pos = (unsigned)MCL(MCL_POS);
    unsigned at = next && pos < size ? pos + 1 : size;
    if (at == size)
        for (unsigned i = 0; i < n; ++i) _deque_push_back(queue, deque_at(add, i));
    else {
        void *all = _create_deque("stSongInfo");
        deque_init(all);
        for (unsigned i = 0; i < size + n; ++i)
            _deque_push_back(all, i < at       ? deque_at(queue, i)
                                  : i < at + n ? deque_at(add, i - at)
                                               : deque_at(queue, i - n));
        deque_clear(queue);
        deque_assign(queue, all);
        deque_destroy(all);
    }
    void *pool = P(MCL_POOL, 0);
    for (unsigned i = 0; i < deque_size(pool); ++i) {
        int *index = deque_at(pool, i);
        if ((unsigned)*index >= at) *index += (int)n;
    }
    for (unsigned i = 0; i < n; ++i) _deque_push_back(pool, (int)(at + i));
    if (MCL(MCL_LASTPOS) != -1 && (unsigned)MCL(MCL_LASTPOS) >= at) MCL(MCL_LASTPOS) += (int)n;
    /* A preload always targets pos+1 (or the wrap); only an insert there makes it stale.
     * Same close as mclSetPlayMode @0x5ab29c. */
    if (MCL(MCL_PRELOAD) == 1 && at == pos + 1) {
        if (MCL(MCL_FD) != -1) send(MCL(MCL_FD), "{mcl-closegapless\\null}", 23, 0);
        MCL(MCL_PRELOAD) = -1;
    }
    /* Shuffle picks at random: the first inserted track is forced once (ringnav_shuffle). */
    if (next) {
        st.qm_forced = MCL(MCL_MODE) == 2 ? at + 1 : 0;
        st.qm_forced_hash = rec_hash(deque_at(queue, at));
    }
}

static int qm_apply(void *add, int next) {
    void *queue = P(mcl_pdeqplaylist, 0);
    if (!queue || airplayGetFlag() == 2) return 0;
    unsigned size = deque_size(queue), type = (unsigned)MCL(MCL_TYPE), n = deque_size(add);
    /* Only a local (folder or library) queue grows; streams keep theirs. */
    if (size && type != 1 && (type & 0xf000) != 0xf000) return 0;
    if (n && !size) /* loads without starting playback */
        mclLoadPlayList(add, 0, st.qm_kind >= QM_FOLDER ? 1 : (int)st.qm_cls);
    else if (n)
        qm_insert(queue, add, size, next);
    return n != 0;
}

static void toast(const char *text) {
    struct {
        int kind, ms;
        char text[0x400];
    } info = { 1, 2000, { 0 } };
    for (unsigned i = 0; text[i]; ++i) info.text[i] = text[i];
    navigator_to_with_context("dialog/msginfo_dialog", &info);
}

static int all_songs(void *unused) {
    (void)unused;
    return getAllMusic(0);
}

/* Shuffle Songs: every song, shuffle on as the play-mode setting saves it, from a random track.
 * ponytail: folder play, as Coverflow's, so a resume after reboot reloads only the last track's
 * folder; the library class needs g_local_classinfo_save built as stock's All Songs does. */
static int shuffle_play(void *all) {
    int size = (int)deque_size(all);
    if (size) {
        config_playmode(2, 1);
        play_folder(all, toolsRandnum(size));
    }
    return size;
}

static int shuffle_songs(void *ctx, void *event) {
    (void)ctx;
    (void)event;
    int n;
    void *all = staged(all_songs, 0, &n);
    if (!shuffle_play(all)) toast("Update Local Music first");
    deque_destroy(all);
    return 0;
}

#define RESUME_FILE "/mnt/data/ringnav-resume" /* coverflow.c's blob_io */
#define PLAYS_FILE "/mnt/data/ringnav-plays"

static void plays_load(void) {
    if (!st.plays_read) {
        st.plays_read = 1;
        BLOB_IO(PLAYS_FILE, st.plays, 0);
    }
}

/* A play count's key: the path's hash, with a CUE track's start mixed in, since a CUE image's
 * tracks share one path. Never 0, which marks a free slot. */
static unsigned listen_key(void *r) {
    unsigned key = fnv(FNV_SEED, P(r, REC_PATH));
    int cue = I(r, REC_CUE_START);
    if (cue) key = hash_bytes(key, (const unsigned char *)&cue, sizeof cue);
    return key | !key;
}

/* Most Played's row: the ranked list from that track, folder-played as Coverflow's. */
static int mp_play(void *ctx, void *event) {
    (void)event;
    play_folder(st.mp_list, (int)(long)ctx);
    return 0;
}

static int mp_keyup(void *ctx, void *event) {
    (void)ctx;
    if (I(event, EVENT_KEY) != KEY_RETURN) return 0;
    navigator_back();
    return STOP;
}

static int mp_closed(void *ctx, void *event) {
    (void)ctx;
    (void)event;
    st.mp_page = (void *)0;
    return 0;
}

/* The PLAYS_TOP most played of songs, their indices in idx, most played first and, among equal
 * counts, most recently counted first (the table keeps that order); returns how many. */
static int mp_rank(void *songs, int *idx, unsigned *score) {
    int top = 0, size = (int)deque_size(songs);
    plays_load();
    for (int i = 0; i < size; i++) {
        unsigned key = listen_key(deque_at(songs, i)), c = 0;
        for (int j = 0; j < PLAYS_SLOTS && !c; j++)
            if (st.plays[j].key == key) c = st.plays[j].n * PLAYS_SLOTS + PLAYS_SLOTS - j;
        if (!c || (top == PLAYS_TOP && score[top - 1] >= c)) continue;
        int at = top < PLAYS_TOP ? top++ : PLAYS_TOP - 1;
        for (; at > 0 && score[at - 1] < c; at--) score[at] = score[at - 1], idx[at] = idx[at - 1];
        score[at] = c;
        idx[at] = i;
    }
    return top;
}

/* Every song, newest first: getAllMusic's sort_time_desc, an SQL ORDER BY, without the pinyin name
 * sort mode 0 runs on every comparison. */
static int songs_unsorted(void *unused) {
    (void)unused;
    return getAllMusic(3);
}

/* Coverflow's Most Played order: every counted song's listens added to sum[album_of(song)], the
 * album's index in Coverflow's list (-1 none). One pass over the library. */
void album_plays(int (*album_of)(void *), unsigned *sum) {
    int n;
    void *songs = staged(songs_unsorted, 0, &n);
    plays_load();
    for (unsigned i = 0; i < deque_size(songs); i++) {
        void *r = deque_at(songs, i);
        unsigned key = listen_key(r), c = 0;
        for (int j = 0; j < PLAYS_SLOTS && !c; j++)
            if (st.plays[j].key == key) c = st.plays[j].n;
        int a = c ? album_of(r) : -1;
        if (a >= 0) sum[a] += c;
    }
    deque_destroy(songs);
}

/* Where key is among Most Played's tracks, -1 when not (or none are kept). */
static int mp_find(unsigned key) {
    for (unsigned i = 0; st.mp_list && i < deque_size(st.mp_list); i++)
        if (listen_key(deque_at(st.mp_list, i)) == key) return (int)i;
    return -1;
}

/* Most Played: a page of the PLAYS_TOP most played songs of the library. The whole library is read
 * only when nothing is kept; otherwise the kept tracks are ranked again.
 * ponytail: the first open after boot or a library change still reads every song, and a song first
 * counted from stock's folder browser shows without its artist until then (no library tags). */
static int most_played(void *ctx, void *event) {
    (void)ctx;
    (void)event;
    if (st.mp_page || !(st.mp_page = page_open("mostplayed_page", mp_closed, mp_keyup))) return 0;
    int n, idx[PLAYS_TOP];
    unsigned score[PLAYS_TOP], gen = library_gen;
    void *from = st.mp_list;
    if (!from || st.mp_gen != gen) {
        from = staged(all_songs, 0, &n);
        st.mp_gen = gen;
    }
    int top = mp_rank(from, idx, score);
    void *ranked = _create_deque("stSongInfo");
    deque_init(ranked);
    for (int i = 0; i < top; i++) _deque_push_back(ranked, deque_at(from, idx[i]));
    if (st.mp_list && st.mp_list != from) deque_destroy(st.mp_list);
    deque_destroy(from);
    st.mp_list = ranked;
    void *view =
        page_list(st.mp_page, st.mp_page, 0, top ? "Most Played" : "No plays yet", top, 64);
    char name[512], detail[300];
    for (int i = 0; i < top; i++) { /* the artist, if tagged, and the play count, under the title */
        void *t = deque_at(st.mp_list, (unsigned)i);
        const char *artist = P(t, REC_ARTIST);
        unsigned plays = (score[i] - 1) / PLAYS_SLOTS, a = artist && *artist;
        tk_snprintf(detail, sizeof detail, a ? "%s · %u play%s" : "%s%u play%s", a ? artist : "",
                    plays, plays == 1 ? "" : "s");
        page_row_detail(view, i, track_name(name, sizeof name, t), detail, mp_play);
    }
    return 0;
}

/* Upload Scrobbles: the log goes up on scrobble.c's thread, and a timer reports how it went. */
static int upload_wait(const void *unused) {
    (void)unused;
    int sent = 0, done = scrobble_poll(&sent);
    if (!done) return 8; /* RET_REPEAT */
    char s[48];
    tk_snprintf(s, sizeof s, done < 0 ? "Upload failed, %d sent" : "Scrobbles sent: %d", sent);
    toast(done > 0 && !sent ? "Nothing to upload" : s);
    return 0;
}

static int upload_scrobbles(void *ctx, void *event) {
    (void)ctx;
    (void)event;
    int started = get_wifisignal() > 0 ? scrobble_start() : -2;
    if (started > 0) timer_add(upload_wait, (void *)0, 500);
    toast(started > 0     ? "Uploading scrobbles"
          : !started      ? "Already uploading"
          : started == -2 ? "Connect to Wi-Fi first"
                          : "Upload failed");
    return 0;
}

/* Podcasts and Audiobooks (docs/internals.md#podcasts-and-audiobooks): the card's top-level
 * folders of these names, case aside, browsed in folder_page from there; Photos opens photos.c,
 * Books and Videos books.c. */
enum { PODCASTS = 1, AUDIOBOOKS, PHOTOS, BOOKS, VIDEOS };
static const char *const MEDIA[] = { "Podcasts", "Audiobooks", "Photos", "Books", "Videos" };

/* 1 + the MEDIA folder s names (up to its end or a '/'), 0 for none. MEDIA is letters only. */
static int media_kind(const char *s) {
    for (int k = 0; k < VIDEOS; k++) {
        unsigned n = strlen(MEDIA[k]);
        if (!strncasecmp(s, MEDIA[k], n) && (!s[n] || s[n] == '/')) return k + 1;
    }
    return 0;
}

/* A track in Podcasts or Audiobooks: always resumed, never counted or scrobbled. */
static int spoken(const char *path) {
    return path && tk_str_start_with(path, "/mnt/mmc/") && media_kind(path + 9) - 1u < AUDIOBOOKS;
}

/* The card's folder for kind into path (sizeof st.media_root, which a MEDIA name always fits); 0
 * when there is none. */
static int media_find(int kind, char *path) {
    void *dir = opendir("/mnt/mmc");
    int found = 0;
    for (struct dirent *e; dir && !found && (e = readdir(dir));)
        if ((e->d_type == 4 || !e->d_type) && media_kind(e->d_name) == kind)
            found = tk_snprintf(path, sizeof st.media_root, "/mnt/mmc/%s", e->d_name) > 0;
    if (dir) closedir(dir);
    return found;
}

static int media_click(void *ctx, void *event) {
    (void)event;
    int kind = (int)(long)ctx;
    char root[sizeof st.media_root];
    if (kind >= PHOTOS) {
        if (!media_find(kind, root)) return 0;
        if (kind == PHOTOS)
            photos_open(root);
        else
            books_open(root, kind == VIDEOS);
        return 0;
    }
    st.media_open = media_find(kind, st.media_root);
    if (st.media_open) navigator_to("folder_page");
    return 0;
}

/* folder_page_init lists the storage roots at g_folder_layer 1, a root's folders at 2 and deeper
 * folders at 3 on (folder_enter). A media root becomes a layer-3 folder, so stock's reload, title
 * (its name, folder_reinit_navbarname) and entering all treat it as one. */
int ringnav_folder(void *win, void *ctx) {
    int result = stock_folder_trampoline(win, ctx);
    if (st.media_open && win) {
        memcpy((char *)g_folder_path, st.media_root, sizeof st.media_root);
        g_folder_layer = 3;
        folder_reload_data();
        folder_reinit_navbarname();
        folder_refresh(win);
    } else
        st.media_root[0] = 0;
    st.media_open = 0;
    return result;
}

/* Back at the media root leaves the page (stock's layer 0) instead of climbing to the card. */
int ringnav_folder_back(void *yoffset, void *index) {
    if (st.media_root[0] && g_folder_layer == 3 &&
        !tk_strcmp((const char *)g_folder_path, st.media_root)) {
        g_folder_layer = 1;
        st.media_root[0] = 0;
    }
    return stock_folder_back_trampoline(yoffset, index);
}

/* A list_row titled text; returns its list_item. */
static void *library_row(void *view, const char *icon, int (*click)(void *, void *), void *ctx,
                         const char *text) {
    void *label = list_row(view, icon, click, ctx);
    widget_set_text_utf8(label, text);
    return P(P(label, W_PARENT), W_PARENT);
}

/* localmusic_page_init: stock's 11 category rows (0x5247ec), by index (get_localmusic_showinfo
 * 0x5012d0): Update Local Music, All Songs, Album, Artist, Genre, Hi-Res, My Fav, Frequent,
 * Recent, Recently Added, Playlist. */
enum {
    L_UPDATE,
    L_SONGS,
    L_ALBUMS,
    L_ARTISTS,
    L_GENRES,
    L_HIRES,
    L_FAV,
    L_FREQUENT,
    L_RECENT,
    L_ADDED,
    L_PLAYLISTS,
    L_STOCK
};

#if IPOD
static void library_stock_key(char *s, int i) { tk_snprintf(s, 32, "_library_stock%d", i); }

static int library_menu_closed(void *ctx, void *event) { (void)ctx; (void)event; return 0; }

static int library_menu_key(void *ctx, void *event) {
    (void)ctx;
    if (I(event, EVENT_KEY) == KEY_RETURN) {
        window_close(window_manager_get_top_window(window_manager()));
        return STOP;
    }
    return 0;
}

static int library_pick(void *item, void *event) {
    (void)event;
    void *page = window_of(item), *wm = window_manager();
    void *owner = (void *)(unsigned)widget_get_prop_int(page, "_library_owner", 0);
    /* The native rows stay owned by Library. A stale submenu cannot dispatch a destroyed row. */
    if (!owner || widget_lookup(wm, "localmusic_page", 0) != owner) return 0;
    if (widget_get_prop_int(page, "_library_generation", 0) !=
        widget_get_prop_int(owner, "_library_generation", 0)) return 0;
    void *target = (void *)(unsigned)widget_get_prop_int(item, "_library_target", 0);
    if (target) {
        char click[0x30];
        stock_dispatch_trampoline(target, pointer_event_init(click, EVT_CLICK, target, 0, 0));
    }
    return 0;
}

static void library_proxy(void *view, int i, const char *caption, void *target) {
    void *item = list_item_create(view, 0, i * 48, 375, 48);
    widget_use_style(item, "s_listitem_black");
    void *label = hscroll_label_create(item, 30, 0, 285, 48);
    widget_use_style(label, "s_scrlabel_white20l");
    set_hscroll_label_attribute(label);
    widget_set_text_utf8(label, caption);
    prop(item, "_library_target", (int)target);
    widget_on(item, EVT_CLICK, library_pick, item);
}

static int library_menu(void *owner, int tools) {
    const char *name = tools ? "librarytools_page" : "libraryhistory_page";
    if (widget_lookup(window_manager(), name, 0)) return 0;
    int upload = widget_get_prop_int(owner, "_library_upload", 0);
    void *page = page_open(name, library_menu_closed, library_menu_key);
    if (!page) return 0;
    prop(page, "_library_owner", (int)owner);
    prop(page, "_library_generation", widget_get_prop_int(owner, "_library_generation", 0));
    void *view = page_list(page, page, 0, tools ? "Library tools" : "Listening history",
                           tools ? 1 + !!upload : 4, 48);
    static const int rows[] = { L_ADDED, L_RECENT, -1, L_FREQUENT, L_UPDATE };
    static const char *const captions[] = {
        "Recently added", "Recently played", "Most played", "Frequent", "Scan music"
    };
    for (int i = tools ? 4 : 0; i < (tools ? 5 : 4); i++) {
        char key[32];
        library_stock_key(key, rows[i]);
        void *row = (void *)(unsigned)widget_get_prop_int(owner, rows[i] < 0 ? "_library_most" : key, 0);
        library_proxy(view, tools ? 0 : i, captions[i], widget_get_child(row, 0));
    }
    if (tools && upload) library_proxy(view, 1, "Upload scrobbles", widget_get_child((void *)upload, 0));
    return 0;
}
static int library_history(void *owner, void *event) { (void)event; return library_menu(owner, 0); }
static int library_tools(void *owner, void *event) { (void)event; return library_menu(owner, 1); }
static int library_search(void *owner, void *event) { return stock_search(owner, event); }
#endif

/* The Library: Shuffle Songs, browsing, the user's own lists, listening history, the card's other
 * media, then upkeep. The added rows' buttons have no name, so stock's row click (atoi of the
 * name, 0x5241fc) never sees them. */
int ringnav_localmusic(void *win, void *ctx) {
    int result = stock_localmusic_trampoline(win, ctx);
    void *view = win ? widget_lookup(win, "scroll_view_localmusic", 1) : (void *)0;
    if (!view || widget_count_children(view) != L_STOCK) return result;
    void *stock[L_STOCK];
    for (int i = 0; i < L_STOCK; i++) stock[i] = widget_get_child(view, i);
    unsigned n = 0; /* each row moves to n as it comes, so Update Local Music, left, ends last */
#if IPOD
    prop(win, "_library_upload", 0);
    prop(win, "_library_generation", (int)++st.library_page_generation);
    for (int i = 0; i < L_STOCK; ++i) {
        char key[32];
        library_stock_key(key, i);
        prop(win, key, (int)stock[i]);
    }
    static const unsigned char primary[] = { L_ARTISTS, L_ALBUMS, L_SONGS, L_PLAYLISTS, L_FAV, L_GENRES };
    for (unsigned i = 0; i < sizeof primary; i++) widget_restack(stock[primary[i]], n++);
    widget_restack(library_row(view, 0, library_search, win, "Search"), n++);
#endif
    widget_restack(library_row(view, "local_shuffle", shuffle_songs, 0, "Shuffle Songs"), n++);
#if IPOD
    void *most = library_row(view, "local_frequentplay", most_played, 0, "Most Played");
    prop(win, "_library_most", (int)most);
    widget_set_visible(most, 0, 0);
    for (unsigned i = 0; i < 4; ++i) {
        static const unsigned char hidden[] = { L_ADDED, L_RECENT, L_FREQUENT, L_UPDATE };
        widget_set_visible(stock[hidden[i]], 0, 0);
    }
    widget_restack(library_row(view, 0, library_history, win, "Listening history"), n++);
#else
    static const unsigned char middle[] = { L_ARTISTS,   L_ALBUMS, L_SONGS, L_GENRES,
                                            L_PLAYLISTS, L_FAV,    L_ADDED, L_RECENT };
    for (unsigned i = 0; i < sizeof middle; i++) widget_restack(stock[middle[i]], n++);
    widget_restack(library_row(view, "local_frequentplay", most_played, 0, "Most Played"), n++);
    widget_restack(stock[L_FREQUENT], n++);
#endif
    widget_restack(stock[L_HIRES], n++);
    /* Podcasts, Audiobooks, Photos, Books and Videos, each only with its folder. */
    static const char *const icons[] = { "local_podcasts", "local_audiobooks", "local_photos",
                                         "local_books", "local_videos" };
    char path[sizeof st.media_root];
    for (int k = 0; k < VIDEOS; k++)
        if (media_find(k + 1, path))
            widget_restack(
                library_row(view, icons[k], media_click, (void *)(long)(k + 1), MEDIA[k]), n++);
    if (scrobble_ready()) {
        void *upload = library_row(view, "local_scrobble", upload_scrobbles, 0, "Upload Scrobbles");
#if IPOD
        prop(win, "_library_upload", (int)upload);
        widget_set_visible(upload, 0, 0);
#else
        widget_restack(upload, n++);
#endif
    }
#if IPOD
    widget_restack(library_row(view, 0, library_tools, win, "Library tools"), n++);
#endif
    return result;
}

/* systemset_about_page_init: stock's seven rows (0x4bc274), each a s_listitem_black list_item
 * holding a 335x70 s_btn_listitem button, a 166px s_scrlabel_white24l title at x 10 and a 149px
 * s_scrlabel_white20r value at x 176, both focus-only scrolling with ellipses. FW. Version (row 1)
 * reads demo's version literal, which carries the updater tag (tools/build.py VERSIONS), so it
 * shows the stock firmware's again; a CFW. Version row in the same widgets follows it. Its button
 * has no name, so stock's row click (atoi of the name, 0x4bc774) ignores it. */
static void about_label(void *button, int x, int w, const char *style, const char *text) {
    void *label = hscroll_label_create(button, x, 0, w, 70);
    widget_use_style(label, style);
    hscroll_label_set_only_focus(label, 1);
    hscroll_label_set_ellipses(label, 1);
    widget_set_text_utf8(label, text);
}

int ringnav_about(void *win, void *ctx) {
    int result = stock_about_trampoline(win, ctx);
    void *view = win ? widget_lookup(win, "scroll_view_about", 1) : (void *)0;
    if (view && widget_count_children(view) == 7) {
        widget_set_text_utf8(widget_get_child(widget_get_child(widget_get_child(view, 1), 0), 1),
                             STOCK_VERSION);
        void *button = list_button(view);
        about_label(button, 10, 166, "s_scrlabel_white24l", "CFW. Version");
        about_label(button, 176, 149, "s_scrlabel_white20r", Q2POD_VERSION);
        widget_restack(P(button, W_PARENT), 2);
    }
    return result;
}

/* Go to album/artist reuse g_local_classinfo_save, which the pages underneath reload from when
 * they come back, so it is put back as the page opened here closes. */
static int qm_restore(void *page, void *event) {
    (void)page;
    (void)event;
    memcpy((void *)g_local_classinfo_save, st.qm_classinfo, sizeof st.qm_classinfo);
    return 0;
}

/* Now Playing's More rows, for this record: Album info (0x5283b0) fills the album query and opens
 * playerjumpinfo_page; Artist info opens artistinfo_page with {class, record}. */
static void qm_goto(void *r, int album) {
    unsigned char *info = (unsigned char *)g_local_classinfo_save;
    void *wm = window_manager(), *page = window_manager_get_top_window(wm);
    memcpy(st.qm_classinfo, info, sizeof st.qm_classinfo);
    if (album) {
        info[9] = 0;
        tk_snprintf((char *)info + 0xd, 0x100, "%s", (const char *)P(r, REC_ALBUM));
        tk_snprintf((char *)info + 0x10d, 0x100, "%s", (const char *)P(r, REC_ARTIST));
        *(int *)info = 0xff10;
        navigator_to("playerjumpinfo_page");
    } else {
        struct {
            int cls;
            void *r;
        } context = { (int)st.qm_cls, r };
        navigator_to_with_context("localmusic/artistinfo_page", &context);
    }
    void *top = window_manager_get_top_window(wm);
    if (top != page) widget_on(top, EVT_WINDOW_CLOSE, qm_restore, top);
}

/* The row alone selected in the stock batch record, as player_addplayList (0x50d0b8) does. */
static void qm_select(void) {
    batch_init_selectrecord((int)st.qm_rows);
    batch_set_selectitem((int)st.qm_idx);
}

/* Deferred so the dialog is never closed under its own click dispatch. */
static int qm_run(const void *unused) {
    (void)unused;
    st.qm_timer = 0;
    if (!st.qm_dialog) return 0;
    qm_close();
    void *r = qm_record();
    int a = st.qm_action, cls = st.qm_kind == QM_COVERFLOW ? 0xf001 : (int)st.qm_cls;
    if (r && a == QA_FAV) { /* tags a folder file as batch-select's Add to My Fav does */
        qm_select();
        batch_add_file(cls, 0xf00a, qm_list(), P(p_vector_select_record, 0), 0);
        toast("Added to Favourites");
    } else if (r && a == QA_UNFAV) {
        deleteMusicFromFav(r);
        if (cls == 0xf00a)
            g_delete_flag = 1; /* My Fav reloads, as Now Playing's heart (0x52b2e8) */
        toast("Removed from Favourites");
    } else if (r && a == QA_PLAYLIST) { /* the playlist page's add mode adds the selected row */
        qm_select();
        navigator_to_with_context("localmusic/playlist_page", (void *)(long)(0x10000 | cls));
    } else if (r && a >= QA_ALBUM)
        qm_goto(r, a == QA_ALBUM);
    else {
        void *add = r ? qm_collect(r) : (void *)0;
        if (!add || !(a == QA_SHUFFLE ? shuffle_play(add) : qm_apply(add, a == QA_NEXT)))
            toast("Queue unchanged");
        if (add) deque_destroy(add);
    }
    return 0;
}

/* Touch and centre both arrive as the row's click; the first one wins. */
static int qm_pick(void *action, void *event) {
    (void)event;
    if (st.qm_dialog && !st.qm_timer) {
        st.qm_action = (int)(long)action;
        st.qm_timer = timer_add(qm_run, (void *)0, 0);
    }
    return 0;
}

/* Deferred out of the long-press dispatch. The sortselect init is synchronous and non-modal
 * (it clears to_modal), so the dialog is the top window once navigator_to returns. */
static int qm_open(const void *unused) {
    (void)unused;
    st.qm_timer = 0;
    void *wm = window_manager(), *page = window_manager_get_top_window(wm);
    void *r = usable() && !window_manager_is_animating(wm) ? qm_record() : (void *)0;
    if (!r) return 0;
    navigator_to("dialog/sortselect_dialog");
    void *dialog = window_manager_get_top_window(wm);
    if (dialog == page) return 0;
    if (tk_strcmp(widget_get_prop_str(dialog, "name", ""), "sortselect_dialog")) {
        window_close(dialog);
        return 0;
    }
    void *back = widget_lookup(dialog, "img_return", 1),
         *view = widget_lookup(dialog, "scroll_view", 1);
    widget_off_by_func(dialog, EVT_KEY_UP, (void *)SORTSELECT_KEYUP, dialog);
    widget_off_by_func(back, EVT_CLICK, (void *)SORTSELECT_CLOSE, dialog);
    widget_on(dialog, EVT_KEY_UP, qm_back, dialog);
    widget_on(back, EVT_CLICK, qm_back, dialog);
    widget_on(dialog, EVT_DESTROY, qm_gone, dialog);
    unsigned cls = st.qm_cls;
    int song = st.qm_kind == QM_SONG || st.qm_kind == QM_COVERFLOW;
    const char *album = P(r, REC_ALBUM), *artist = P(r, REC_ARTIST);
    const char *title = (st.qm_kind == QM_ALBUM || st.qm_kind == QM_COVERALBUM) ? album
                        : st.qm_kind == QM_GROUP ? P(r, qm_by[(cls & 0xf) - 4])
                                                 : P(r, REC_NAME);
    widget_set_text_utf8(widget_lookup(dialog, "scrlabel_title", 1), title ? title : "");
    widget_destroy_children(view);
    /* Songs favourite, as stock's heart, and go to their album and artist; collections shuffle.
     * Go to is left out where its single-instance page is already open, or the album is the page.
     */
    unsigned char acts[6], n = 0;
    acts[n++] = QA_NEXT;
    acts[n++] = QA_ADD;
    if (!song) acts[n++] = QA_SHUFFLE;
    if (song) acts[n++] = checkFavExist(r) ? QA_UNFAV : QA_FAV;
    if (st.qm_kind < QM_COVERFLOW) acts[n++] = QA_PLAYLIST;
    if (st.qm_kind == QM_SONG && album && *album && (cls & 0xfff0) != 0xff10 &&
        !navigator_window_is_exist("playerjumpinfo_page"))
        acts[n++] = QA_ALBUM;
    if ((song || cls == CLASS_ALBUMS) && artist && *artist &&
        !navigator_window_is_exist("artistinfo_page"))
        acts[n++] = QA_ARTIST;
    static const char *const rows[] = { "Play next",
                                        "Add to queue",
                                        "Shuffle",
                                        "Add to Favourites",
                                        "Remove from Favourites",
                                        "Add to playlist",
                                        "Go to album",
                                        "Go to artist" };
    for (int i = 0; i < n; ++i) { /* stock sortselect row geometry and styles */
        void *item = list_item_create(view, 0, i * 78, 375, 78);
        widget_use_style(item, "s_listitem_black");
        void *label = hscroll_label_create(item, 30, 0, 266, 70);
        widget_use_style(label, "s_scrlabel_white24l");
        widget_set_text_utf8(label, rows[acts[i] - 1]);
        widget_on(item, EVT_CLICK, qm_pick, (void *)(long)acts[i]);
    }
    widget_set_prop_int(view, "virtual_h", n * 78);
    st.qm_dialog = dialog;
    return 0;
}

/* Centre hold: the same gates and row as a short press, over the stock showlist or Coverflow's
 * own tracks (row count checked). Everything else stays stock. */
static int qm_hold(void) {
    void *wm = window_manager(), *top = window_manager_get_top_window(wm);
    if (st.qm_dialog || st.qm_timer || !usable() || !allowed_top(top) ||
        window_manager_is_animating(wm) || window_manager_get_pointer_pressed(wm) ||
        airplayGetFlag() == 2 || g_navbar_status)
        return 0;
    const char *name = widget_get_prop_str(top, "name", "");
    int kind = contexts[context_id(name)].kind;
    void *album = coverflow_album(top);
    char *key = input_key(KEY_CENTER);
    if (album) {
        if (!key || !(st.qm_timer = timer_add(qm_open, (void *)0, 0))) return 0;
        st.qm_kind = QM_COVERALBUM;
        st.qm_cls = CLASS_ALBUMS;
        st.qm_hash = rec_hash(album);
        st.hold_press = *(unsigned long long *)((char *)key + INPUT_KEY_TIME);
        drop_input();
        return 1;
    }
    void *list = page_tracks(top);
    int coverflow = list != 0;
    void *w = surface_under(top, (void *)0, (void *)0, 0);
    if ((!coverflow && kind < CTX_FOLDER) || !w || !load(&g_menu, w, 1) || g_menu.ctx < 0 ||
        g_menu.kind == 3)
        return 0;
    int cur = reconcile(&g_menu, !moving(&g_menu));
    if (!coverflow) list = P(p_deque_showlist, 0);
    if (cur < 0 || !list || deque_size(list) != (unsigned)g_menu.rows) return 0;
    void *r = deque_at(list, g_menu.id[cur]);
    /* An artist's tabs reload the showlist with load_localartist_list, which sets classinfo +0
     * (0xff07 songs, 0xff01 albums) but not g_class_type. */
    unsigned cls = (unsigned)(tk_strcmp(name, "artistinfo_page") ? I(g_class_type, 0)
                                                                 : I(g_local_classinfo_save, 0));
    if (!r || !key) return 0;
    st.qm_kind = I(r, REC_TYPE) == 8 ? QM_SONG : 0;
    if (coverflow) {
        if (st.qm_kind) st.qm_kind = QM_COVERFLOW;
    } else if (kind == CTX_FOLDER) {
        if (I(r, REC_TYPE) == 4) st.qm_kind = QM_FOLDER;
    } else if (cls == CLASS_ALBUMS || (cls >= CLASS_ARTIST_ALBUMS && cls <= 0xff03))
        st.qm_kind = QM_ALBUM; /* all albums, and an artist's, composer's or genre's */
    else if (cls >= 0xf004 && cls <= 0xf006)
        st.qm_kind = QM_GROUP; /* artist, composer and genre lists */
    if (!st.qm_kind || !(st.qm_timer = timer_add(qm_open, (void *)0, 0))) return 0;
    st.qm_cls = cls;
    st.qm_idx = (unsigned)g_menu.id[cur];
    st.qm_rows = (unsigned)g_menu.rows;
    st.qm_hash = rec_hash(r);
    st.qm_browse = browse_hash();
    st.hold_press = *(unsigned long long *)((char *)key + INPUT_KEY_TIME);
    drop_input();
    return 1;
}

/* The hidden More widget retains stock's menu callback and current queue context. */
static int player_more_hold(void) {
    void *wm = window_manager(), *top = window_manager_get_top_window(wm);
    if (!usable() || !top || window_manager_is_animating(wm) ||
        window_manager_get_pointer_pressed(wm) ||
        tk_strcmp(widget_get_prop_str(top, "name", ""), "playing_page")) return 0;
    void *button = widget_lookup(top, "img_more", 1);
    char *record = input_key(KEY_CENTER);
    if (!record || !button || !widget_get_prop_bool(button, "enable", 1)) return 0;
    st.hold_press = *(unsigned long long *)(record + INPUT_KEY_TIME);
    drop_input();
    char click[0x30];
    stock_dispatch_trampoline(button, pointer_event_init(click, EVT_CLICK, button, 0, 0));
    return 1;
}

/* Centre opens player or row actions; shutdown lives in Power management. */
int ringnav_keylong(void *ctx, void *event) {
    unsigned key = event ? (unsigned)I(event, EVENT_KEY) : 0;
    if (key == KEY_CENTER) {
        np_cancel();
        if (!player_more_hold() && !qm_hold()) {
            char *record = input_key(KEY_CENTER);
            if (record) st.hold_press = *(unsigned long long *)(record + INPUT_KEY_TIME);
            drop_input();
        }
        return STOP;
    }
    if (key == KEY_PLAY && usable() && !window_manager_is_animating(window_manager()) &&
        !window_manager_get_pointer_pressed(window_manager())) {
        char *record = input_key(KEY_PLAY);
        if (record) {
            st.hold_press = *(unsigned long long *)(record + INPUT_KEY_TIME);
            open_now_playing();
            return STOP;
        }
    }
    return stock_keylong_trampoline(ctx, event);
}

/* Replaces mclNextSong's shuffle pick call: stock picks and bookkeeps, then a pending Play next
 * becomes the next track if it is still at its index. */
int ringnav_shuffle(int forward) {
    int result = mcl_shuffle_pick(forward);
    void *queue = P(mcl_pdeqplaylist, 0);
    unsigned at = st.qm_forced - 1;
    st.qm_forced = 0;
    if (queue && at < deque_size(queue) && rec_hash(deque_at(queue, at)) == st.qm_forced_hash)
        MCL(MCL_POS) = (int)at;
    return result;
}

/* Replaces the name comparators' toolsTrimLeft calls (SORT_TRIMS): each trims its own copy of a
 * name, which then sorts without its article. */
void ringnav_sort_key(char *s) {
    toolsTrimLeft(s);
    unsigned n = article(s);
    if (n)
        for (char *d = s; (*d = d[n]); ++d) {}
}

/* Moves key's place to the front of the ring at sec, or forgets it near either end. */
static void spot_keep(unsigned key, int sec, int total) {
    int i = 0, keep = sec >= RESUME_EDGE_S && sec < total - RESUME_EDGE_S;
    while (i < RESUME_SLOTS - 1 && st.spots[i].key != key) i++;
    /* nothing to do: already first at sec, or nothing kept to forget */
    if (keep ? !i && st.spots[0].key == key && st.spots[0].sec == sec : st.spots[i].key != key)
        return;
    if (keep) {
        for (; i > 0; i--) st.spots[i] = st.spots[i - 1]; /* the oldest falls off the end */
        st.spots[0].key = key;
        st.spots[0].sec = sec;
    } else {
        for (; i < RESUME_SLOTS - 1; i++) st.spots[i] = st.spots[i + 1];
        st.spots[i].key = 0;
    }
    BLOB_IO(RESUME_FILE, st.spots, 1);
}

/* Counts key's play and moves it first, so among equal counts the least recently played is the
 * one replaced when no slot is free. */
static void play_count(void *r, unsigned key) {
    plays_load();
    int i = 0;
    for (int j = 0; j < PLAYS_SLOTS && st.plays[i].key != key; j++)
        if (st.plays[j].key == key || st.plays[j].n <= st.plays[i].n) i = j;
    /* Most Played's kept tracks take a newly counted song, unless its page shows them (its rows
     * and song menu match the deque); one that loses its count may have hidden the next best,
     * which only the whole library knows. */
    if (st.plays[i].key && st.plays[i].key != key && mp_find(st.plays[i].key) >= 0)
        st.mp_gen = ~library_gen;
    else if (st.mp_list && mp_find(key) < 0) {
        if (!st.mp_page && deque_size(st.mp_list) < 2 * PLAYS_TOP)
            _deque_push_back(st.mp_list, r);
        else
            st.mp_gen = ~library_gen;
    }
    unsigned n = st.plays[i].key == key ? st.plays[i].n + 1 : 1;
    for (; i > 0; i--) st.plays[i] = st.plays[i - 1];
    st.plays[0].key = key;
    st.plays[0].n = n;
    BLOB_IO(PLAYS_FILE, st.plays, 1);
}

/* Scrobbling (docs/internals.md#scrobbling): a Rockbox-style AudioScrobbler 1.1 log at the card's
 * root, for any .scrobbler.log uploader or Upload Scrobbles (scrobble.c). Artist and album are the
 * player's parsed tags when it has parsed this track (now_tag); untagged (artist-less) tracks are
 * skipped, as scrobblers reject them. */
static char *scrobble_tag(char *o, char *end, const char *s) {
    for (; s && *s && o < end - 1; s++) *o++ = *s == '\t' || *s == '\n' || *s == '\r' ? ' ' : *s;
    *o++ = '\t';
    return o;
}

static void scrobble(void *r, int total, int heard) {
    const char *artist = now_tag(r, REC_ARTIST);
    if (!artist || !*artist) return;
    long now = time((void *)0);
    if (now < 1600000000) return; /* clock never set: Last.fm would reject the time */
    char line[800], name[512], *end = line + sizeof line - 48, *o = line; /* 48: the numbers */
    o = scrobble_tag(o, end, artist);
    o = scrobble_tag(o, end, now_tag(r, REC_ALBUM));
    const char *title = P(r, REC_TITLE);
    o = scrobble_tag(o, end, title && *title ? title : track_name(name, sizeof name, r));
    if (I(r, REC_TRACK) > 0) o += tk_snprintf(o, 12, "%d", I(r, REC_TRACK));
    o += tk_snprintf(o, (unsigned)(line + sizeof line - o), "\t%d\tL\t%d\t\n", total,
                     (int)now - heard);
    scrobble_append(line, (unsigned)(o - line));
}

/* Once a second from the UI loop: a long track saves its place and, when it starts playing near
 * its beginning, jumps to the saved one. A track change waits a poll, so the previous track's
 * time never counts for the new one. */
static void resume_poll(void) {
    unsigned now = (unsigned)time_now_ms(), at, n;
    if (now - st.rs_at < 1000) return;
    st.rs_at = now;
    int sec = 0, total = 0;
    void *r = queue_now(&at, &n);
    if (!r || player_playtime_and_length(&sec, &total) < 0 || total <= 0) return;
    unsigned key = fnv(FNV_SEED, P(r, REC_PATH));
    if (key != st.rs_key) {
        if (st.rs_long && !st.rs_pending) spot_keep(st.rs_key, st.rs_sec, st.rs_total);
        st.rs_key = key;
        st.rs_long = 0;
        st.rs_settle = 1;
        return;
    }
    if (st.rs_settle) {
        st.rs_settle = 0;
        st.rs_spoken = spoken(P(r, REC_PATH));
        st.rs_long =
            (total >= RESUME_MIN_S || st.rs_spoken) && !I(r, REC_CUE_START) && !I(r, REC_CUE_END);
        st.rs_pending = 0;
        if (st.rs_long && !st.spots_read) {
            st.spots_read = 1;
            BLOB_IO(RESUME_FILE, st.spots, 0);
        }
        for (int i = 0; st.rs_long && i < RESUME_SLOTS; i++)
            if (st.spots[i].key == key) st.rs_pending = st.spots[i].sec;
        st.rs_sec = st.rs_saved = sec;
        st.rs_still = 0;
    }
    /* A listen: half the track or LISTEN_MAX_S heard, seeks and pauses aside; a repeat starts over.
     */
    if (!st.rs_pending && !st.rs_spoken) {
        unsigned lk = listen_key(r);
        if (lk != st.ls_key || (sec < 2 && st.ls_sec > LISTEN_MIN_S)) {
            st.ls_key = lk;
            st.ls_sec = sec;
            st.ls_heard = st.ls_done = 0;
        }
        int d = sec - st.ls_sec;
        if (d > 0 && d <= 2) st.ls_heard += d;
        st.ls_sec = sec;
        if (!st.ls_done && total > LISTEN_MIN_S &&
            st.ls_heard >= (total / 2 < LISTEN_MAX_S ? total / 2 : LISTEN_MAX_S)) {
            st.ls_done = 1;
            play_count(r, lk);
            scrobble(r, total, st.ls_heard);
        }
    }
    if (!st.rs_long) return;
    st.rs_total = total;
    if (st.rs_pending) {
        if (sec < 1) return; /* not playing yet: a paused boot resume waits for Play */
        if (sec < RESUME_START_S) {
            player_seek_time(st.rs_pending); /* the stock seek can block the UI for up to 2 s */
            sec = st.rs_pending;
        }
        st.rs_pending = 0;
        st.rs_sec = st.rs_saved = sec;
        return;
    }
    st.rs_still = sec == st.rs_sec ? st.rs_still + 1 : 0;
    st.rs_sec = sec;
    int moved = sec - st.rs_saved;
    if (moved && (st.rs_still >= 2 || moved >= RESUME_SAVE_S || moved <= -RESUME_SAVE_S)) {
        st.rs_saved = sec;
        spot_keep(key, sec, total);
    }
}

/* Charge limit (docs/internals.md#charge-limit): get_battery_capacity keeps g_power_capacity and
 * the BQ25890's g_power_chargestate (1 pre-charge, 2 fast charge, else 0); switch_charge_enable
 * drives its /CE pin, as stock's USB mode does with g_usbdac_chargeflag. At CHARGE_STOP charging
 * stops, and again whenever something else (USB mode's exit, AirPlay) turned it back on, until
 * the level falls to CHARGE_RESUME; then, or with the limit off, charging is handed back as stock
 * would have it: USB mode's and AirPlay's own choice while their page is open, else on. */
static void charge_poll(void) {
    unsigned now = time_now_ms();
    if (st.charge_at && now - st.charge_at < CHARGE_POLL_MS) return;
    st.charge_at =
        now | !now; /* never 0, no poll yet; |1 would eat a millisecond when now is even */
    int level = I(g_power_capacity, 0), charging = (unsigned)I(g_power_chargestate, 0) - 1 < 2;
    int hold = st.charge_limit && level > CHARGE_RESUME && (st.charge_held || level >= CHARGE_STOP);
    if (hold && (!st.charge_held || charging))
        switch_charge_enable(0);
    else if (!hold && st.charge_held)
        switch_charge_enable(navigator_window_is_exist("usbmode_page") ||
                                     navigator_window_is_exist("airplay_page")
                                 ? g_usbdac_chargeflag
                                 : 1);
    st.charge_held = hold;
}

/* Low power's second core (docs/internals.md#low-power): CPU1 is offline while the screen is off
 * and no video plays, so the player decodes on one core and the UI, Coverflow and videos keep both.
 * The first offline of a boot is bracketed by CPU1_PENDING, synced to flash: if a kernel stalled on
 * it, the next boot finds the file, renames it CPU1_BAD and never tries again. A write that fails
 * (no hotplug) stops the attempts until the next boot. */
#define CPU1_ONLINE "/sys/devices/system/cpu/cpu1/online"
#define CPU1_PENDING "/mnt/data/q2pod-cpu1"
#define CPU1_BAD "/mnt/data/q2pod-cpu1.bad"
static int cpu1_write(int on) {
    void *f = fopen(CPU1_ONLINE, "w");
    if (!f) return 0;
    int ok = fwrite(on ? "1" : "0", 1, 1, f) == 1;
    return !fclose(f) && ok;
}
static void cpu_poll(void) {
    if (!st.low_power && !st.cpu_off) return; /* off: no file is touched */
    if (!st.cpu_bad) {                        /* once a boot */
        if (!access(CPU1_PENDING, 0)) rename(CPU1_PENDING, CPU1_BAD);
        st.cpu_bad = access(CPU1_BAD, 0) ? 1 : 2;
    }
    int off = st.low_power && !g_backlight_status && !video_on() && st.cpu_bad == 1;
    if (off == st.cpu_off) return;
    unsigned now = time_now_ms();
    if (!off) { /* a refused online is retried once a second */
        if (st.cpu_retry && now - st.cpu_retry < 1000) return;
        st.cpu_off = !cpu1_write(1);
        st.cpu_retry =
            st.cpu_off ? now | !now : 0; /* never 0; |1 would eat a millisecond when now is even */
        return;
    }
    if (!st.cpu_marked) { /* no marker on flash, no offline: the guard must hold */
        void *mark = fopen(CPU1_PENDING, "w");
        int synced = mark && !fflush(mark) && !fsync(fileno(mark));
        if (mark) fclose(mark);
        if (!synced) {
            st.cpu_bad = 3;
            return;
        }
    }
    st.cpu_off = cpu1_write(0);
    if (!st.cpu_marked) {
        unlink(CPU1_PENDING);
        int dir = open("/mnt/data", 0); /* O_RDONLY: the unlink reaches flash too */
        if (dir >= 0) fsync(dir), close(dir);
        st.cpu_marked = 1;
    }
    if (!st.cpu_off) st.cpu_bad = 3;
}
static void power_poll(void) {
    pod_settings();
    charge_poll();
    cpu_poll();
}

/* Now Playing on top: its visualizer, progress and lyrics run on timers, which an idle pass would
 * make late, so Low power leaves the loop at stock's pace there. */
static int now_playing(void) {
    void *top = window_manager_get_top_window(window_manager());
    return top && !tk_strcmp(widget_get_prop_str(top, "name", ""), "playing_page");
}

/* main_loop_sleep_default paces the UI loop at 8 ms (125 Hz), screen on or off. With the backlight
 * off it first idles SCREEN_OFF_SLEEP_MS (Low power: LOW_OFF_SLEEP_MS); stock then finds its 8 ms
 * gone, sleeps 0 and keeps its own bookkeeping. The first pass with it back on repaints every
 * window once, so nothing drawn while dark, or only partly, stays on screen until the next input.
 * With Low power and the screen on, a pass idles LOW_IDLE_SLEEP_MS once input and window
 * animations have been still for LOW_IDLE_MS, except on Now Playing. */
int ringnav_sleep(void *loop) {
    video_poll();
    resume_poll();
    power_poll();
    if (!g_backlight_status) {
        st.dark = 1;
        sleep_ms(st.low_power ? LOW_OFF_SLEEP_MS : SCREEN_OFF_SLEEP_MS);
    } else if (st.dark) {
        st.dark = 0;
        widget_invalidate_force(window_manager(), (void *)0);
    } else if (st.low_power && time_now_ms() - st.last_input >= LOW_IDLE_MS &&
               !window_manager_is_animating(window_manager()) && !now_playing())
        sleep_ms(LOW_IDLE_SLEEP_MS);
    return stock_sleep_trampoline(loop);
}

/* window_manager_dispatch_input_event: while q2video plays (books.c) no key or touch reaches the
 * UI; a key's release goes to the player instead. Every event times Low power's idle. */
int ringnav_input(void *wm, void *e) {
    st.last_input = time_now_ms();
    if (!video_on()) return stock_input_trampoline(wm, e);
    if (e && I(e, EVENT_TYPE) == EVT_KEY_UP) video_key((unsigned)I(e, EVENT_KEY));
    return 0;
}

/* window_manager_paint, a leaf (tools/build.py WM_PAINT_LEAF): the window manager's own paint
 * (vtable +0xc), except while q2video has the framebuffer; video_poll repaints after. */
int ringnav_wm_paint(void *wm) {
    void *vt = wm && !video_on() ? P(wm, 0x94) : (void *)0;
    int (*paint)(void *) = vt ? (int (*)(void *))P(vt, 0xc) : (int (*)(void *))0;
    return paint ? paint(wm) : video_on() ? 0 : 0x10; /* RET_BAD_PARAMS, as stock */
}

/* buzzeer_switch (0x4f3cc8), every Key Tone click, stock's and ringnav()'s: the MCU's buzzer
 * (system("cmd_mcu write_str buzzer") when g_keytone_flag is set). Silent while music plays, and
 * while headphones (either jack) or a Bluetooth or USB output listen, since the buzzer is the
 * device's own speaker; paused hciplayer holds their PCM, so the click has no other way out. */
int ringnav_buzzer(int on) {
    if (mclGetPlayStatus() == 2 || g_po_status || g_bal_status || mclGetOutputWay()) return 0;
    return stock_buzzer_trampoline(on);
}

#if IPOD
/* on_wm_keydown_before_fun. Stock clicks (buzzeer_switch, which reads g_keytone_flag) on the press,
 * before the release moves anything. A wheel press on a page whose release ringnav() takes as row
 * navigation runs the whole stock body with the flag off for that one synchronous call, so its
 * gates and latches still run, and the release clicks when the selection changes. The test reads
 * the tree only: nothing is loaded, selected or restored. Carousels, the pixel-scroll fallback,
 * lyrics, the volume and every other key keep the stock click. */
int ringnav_keydown(void *ctx, void *event) {
    unsigned key = event ? (unsigned)I(event, EVENT_KEY) : 0;
    unsigned char tone = g_keytone_flag;
    void *top = window_manager_get_top_window(window_manager()), *row = (void *)0;
    entries_t s = { &row, 0, 1, 4096 };
    st.tone_key = 0;
    if ((key == KEY_PREV || key == KEY_NEXT) && usable() && allowed_top(top)) {
        void *w = surface_under(top, (void *)0, (void *)0, 1);
        unsigned n = w && kind(w) != 3 ? widget_count_children(w) : 0;
        for (unsigned i = 0; i < n && !s.n; ++i) collect(widget_get_child(w, i), &s, 1);
        if (s.n) st.tone_key = key;
    }
    if (st.tone_key) g_keytone_flag = 0;
    int result = stock_keydown_trampoline(ctx, event);
    if (st.tone_key) g_keytone_flag = tone;
    return result;
}
#endif

static int slider_setting(const char *name) {
    return !tk_strcmp(name, "backlight_page") || !tk_strcmp(name, "maxvol_page") ||
           !tk_strcmp(name, "bootvol_page") || !tk_strcmp(name, "balance_page");
}

static int editor_closed(void *page, void *event) {
    (void)event;
    if (page == st.editor_page) drop_input();
    return 0;
}

static int editor_finish(const void *info) {
    (void)info;
    void *page = st.editor_page;
    st.editor_timer = 0;
    st.editor_page = (void *)0;
    void *wm = window_manager();
    if (!page || !usable() || window_manager_get_top_window(wm) != page ||
        window_manager_is_animating(wm) || window_manager_get_pointer_pressed(wm))
        return 0;
    int count = field_count(page);
    if (!count)
        navigator_back();
    else {
        int field = field_index(page);
        if (field != widget_get_prop_int(page, "_editor_field", -1)) return 0;
        void *target = field_target(page, field);
        if (!control_available(target, page)) return 0;
        if (field == count) {
            char click[0x30];
            *(volatile unsigned char *)KEY_LOCKOUT = 0;
            stock_dispatch_trampoline(target, pointer_event_init(click, EVT_CLICK, target, 0, 0));
        } else {
            *(volatile unsigned char *)KEY_LOCKOUT = 0;
            prop(page, "_wheel_field", ++field);
            if (count == 5 && field < count) {
                void *slide = widget_lookup(page, "slide_view", 1);
                if (slide) widget_set_prop_int(slide, "value", field >= 3);
            }
            widget_invalidate_force(page, (void *)0);
        }
    }
    return 0;
}

static int editor_center(void *page, void *event) {
    void *wm = window_manager();
    unsigned now = (unsigned)time_now_ms();
    if (window_manager_is_animating(wm) || window_manager_get_pointer_pressed(wm)) {
        drop_input();
        return STOP;
    }
    if (st.editor_timer && st.editor_page == page) {
        if (now - st.editor_at < DOUBLE_CLICK_MS) {
            drop_input();
            on_wm_keyup_fun(wm, event); /* the stock double-Centre screen-off path */
            return STOP;
        }
        unsigned timer = st.editor_timer;
        editor_finish(0); /* Apply an overdue single press before starting the next one. */
        timer_remove(timer);
        if (!usable() || window_manager_get_top_window(wm) != page) return STOP;
    }
    drop_input();
    if (!widget_get_prop_int(page, "_editor_bound", 0)) {
        if (!widget_on(page, EVT_DESTROY, editor_closed, page)) return STOP;
        prop(page, "_editor_bound", 1);
    }
    st.editor_page = page;
    st.editor_at = now;
    if (field_count(page)) prop(page, "_editor_field", field_index(page));
    st.editor_timer = timer_add(editor_finish, 0, DOUBLE_CLICK_MS);
    if (!st.editor_timer) {
        if (field_count(page)) drop_input();
        else editor_finish(0);
    }
    return STOP;
}

static int fields_key(void *page, unsigned key, void *event) {
    int count = field_count(page);
    if (!count) return 0;
    if (key == KEY_CENTER) return editor_center(page, event);
    drop_input();
    if (window_manager_is_animating(window_manager()) ||
        window_manager_get_pointer_pressed(window_manager()))
        return STOP;
    int field = field_index(page);
    if (field == count) {
        field = key == KEY_PREV ? count - 1 : 0;
        prop(page, "_wheel_field", field);
        if (count == 5) {
            void *slide = widget_lookup(page, "slide_view", 1);
            if (slide) widget_set_prop_int(slide, "value", field >= 3);
        }
    } else {
        void *target = field_target(page, field);
        if (control_available(target, page)) {
            unsigned options = text_selector_count_options(target);
            int index = I(target, SELECTOR_INDEX);
            if (options && options <= 0x7fffffff) {
                int next = clamp_step(index, (int)options - 1, key == KEY_NEXT ? 1 : -1);
                if (next != index) text_selector_set_selected_index(target, (unsigned)next);
            }
        }
    }
    st.touch_mode = 0;
    widget_invalidate_force(page, (void *)0);
    return STOP;
}

/* Slider-only settings use their stock +/- actions, including validation and persistence.
 * Quick Settings has no +/- buttons; setting its slider fires the stock value-changed callback. */
static int settings_wheel(void *top, unsigned key) {
    if (!top || (key != KEY_PREV && key != KEY_NEXT)) return 0;
    const char *name = widget_get_prop_str(top, "name", "");
    int quick = !tk_strcmp(name, "statusbar_dialog");
    if (!quick && !slider_setting(name)) return 0;
    void *wm = window_manager();
    if (window_manager_is_animating(wm) || window_manager_get_pointer_pressed(wm)) return 1;
    if (quick) {
        void *slider = widget_lookup(top, "slider_backlight", 1);
        if (!slider || !widget_get_visible(slider) || !widget_get_prop_bool(slider, "enable", 1))
            return 1;
        int value = widget_get_prop_int(slider, "value", 0);
        int min = widget_get_prop_int(slider, "min", 0), max = widget_get_prop_int(slider, "max", 100);
        int next = value + (key == KEY_NEXT ? 1 : -1);
        if (next < min) next = min;
        if (next > max) next = max;
        if (next != value) widget_set_prop_int(slider, "value", next);
        return 1;
    }
    void *button = widget_lookup(top, key == KEY_NEXT ? "img_add" : "img_dec", 1);
    if (button && widget_get_prop_bool(button, "enable", 1)
#if !IPOD
        && widget_get_visible(button)
#endif
    ) {
        char click[0x30];
        stock_dispatch_trampoline(button, pointer_event_init(click, EVT_CLICK, button, 0, 0));
    }
    return 1;
}

int ringnav(void *ctx, void *event) {
    pull_cancel();
    /* The stock filter dereferences the event before returning. */
    if (!event) {
        drop_input();
        return 0;
    }
    unsigned key = (unsigned)I(event, EVENT_KEY);
    if (key != KEY_CENTER) {
        stop_timer(&st.editor_timer);
        st.editor_page = (void *)0;
    }
    int waking_center = key == KEY_CENTER && (!g_backlight_status || g_lockscreen_pageflag);
#if IPOD
    /* This release's press was silenced by ringnav_keydown: a row change below clicks instead. */
    int owned = st.tone_key == key;
    st.tone_key = 0;
#endif
    if (key != KEY_PREV && key != KEY_NEXT) st.wheel_tick = 0; /* a button ends the run */
    int result = stock_keyup_trampoline(ctx, event);
    if ((key == KEY_PLAY || key == KEY_CENTER) && hold_released(key)) return STOP;
    if (waking_center) {
        pod_settings();
        unsigned now = (unsigned)time_now_ms();
        if (st.single_wake || (st.unlock_waiting && now - st.unlock_at <= WAKE_DOUBLE_CLICK_MS)) {
            st.unlock_waiting = 0;
            if (!g_backlight_status) on_wm_keyup_fun(ctx, event);
            if (g_lockscreen_pageflag) navigator_back(); /* the close callback clears the flag */
        } else {
            st.unlock_at = now;
            st.unlock_waiting = 1;
        }
        cancel_center();
        drop_spin();
        return STOP;
    }
    if (key != KEY_CENTER) st.unlock_waiting = 0;
#if IPOD
    /* Choice screens own both directions, even during stock's opening-press lockout. */
    if (key == KEY_PREV || key == KEY_NEXT) {
        void *top = window_manager_get_top_window(window_manager());
        if (selection_window(top)) {
            if (!usable()) {
                drop_input();
                return STOP;
            }
            result = 0;
        }
    }
#endif
    if ((key == KEY_PREV || key == KEY_NEXT) &&
        field_count(window_manager_get_top_window(window_manager())) && usable())
        result = 0;
    if (result) {
        cancel_center();
        if (key == KEY_PREV || key == KEY_NEXT) drop_wheel();
        /* Keep the home interval independent of rejected list navigation. */
        if (key == KEY_CENTER || !usable()) drop_spin();
        return result;
    }
#if IPOD
    /* Back by button: the page behind shows its row. A sliding page is painted once more, into its
     * closing snapshot, inside stock's handling of this release; it keeps hiding its own row until
     * that is done. */
    if (key == KEY_RETURN && st.touch_mode) {
        rearm(&st.untouch_timer, untouch, 0);
        if (!st.untouch_timer) st.touch_mode = 0;
    }
#else
    if (key == KEY_RETURN) st.touch_mode = 0; /* Back by button: the page behind shows its row. */
#endif
    if (key != KEY_CENTER && key != KEY_PREV && key != KEY_NEXT) return result;
    if (key == KEY_CENTER) {
        drop_spin();
        fx_cancel();
    } else
        cancel_center();
    if (st.center_timer && (unsigned)time_now_ms() - st.last_center >= DOUBLE_CLICK_MS) {
        unsigned timer = st.center_timer;
        confirm_center((void *)0);
        timer_remove(timer);
    }
    /* An overdue click may change power/lock state; inspect it after its callback. */
    if (!usable()) {
        drop_input();
        return result;
    }
    /* Match the stock power-key release exclusions, including release after long press. */
    if (key == KEY_CENTER &&
        (g_power_longkey || g_ingore_bootkey_flag || *(volatile unsigned char *)BOOT_KEY_GUARD)) {
        cancel_center();
        return result;
    }
    void *wm = window_manager(), *top = window_manager_get_top_window(wm);
    if (settings_wheel(top, key)) {
        drop_input();
        np_cancel();
        return STOP;
    }
    if (field_count(top)) return fields_key(top, key, event);
#if IPOD
    if (key == KEY_CENTER && top && slider_setting(widget_get_prop_str(top, "name", ""))) {
        np_cancel();
        return editor_center(top, event);
    }
    int playing = st.np_win && top == st.np_win;
    if (st.np_win && top &&
        !tk_strcmp(widget_get_prop_str(top, "name", ""), "volume_dialog")) {
        unsigned windows = widget_count_children(wm);
        playing = windows >= 2 && widget_get_child(wm, windows - 2) == st.np_win;
    }
    if (!playing || window_manager_is_animating(wm) || window_manager_get_pointer_pressed(wm)) {
        np_cancel();
        if (playing && (key == KEY_CENTER ||
                        (st.np_panel && (key == KEY_PREV || key == KEY_NEXT)))) return STOP;
    } else if (np_wheel(top, key))
        return STOP;
    else if (key != KEY_CENTER && top == st.np_win && np_lyrics(key))
        return STOP;
    else if (key == KEY_CENTER)
        return np_key(key);
#endif
    if (!allowed_top(top)) {
        drop_input();
        return result;
    }
    if (window_manager_is_animating(wm) || window_manager_get_pointer_pressed(wm)) {
        drop_input();
        return STOP;
    }
    if (books_key(top, key)) return STOP; /* the reader's pages and caption */
    int dir = key == KEY_NEXT ? 1 : key == KEY_PREV ? -1 : 0;
    void *w = surface_under(top, (void *)0, (void *)0, dir != 0);
    if (!is_home(top, w) || w != st.home_surface) st.home_surface = (void *)0;
    if (!w || !load(&g_menu, w, 1)) {
        drop_input();
        return dir ? STOP : result;
    }
    if (st.center_timer && !pending_matches(top, &g_menu)) cancel_center();
    int touch = widget_get_prop_int(w, TOUCH, 0);
    if (touch) {
        stop_scroll(&g_menu);
        prop(w, TOUCH, 0); /* Centre consumes the interrupted gesture too. */
    }
    int cur = reconcile(&g_menu, touch || !moving(&g_menu));
    unsigned now = (unsigned)time_now_ms();
    if (!dir) {
        if (st.center_timer && now - st.last_center < DOUBLE_CLICK_MS) {
            cancel_center();
            return result; /* Let the stock downstream short-press handler turn the screen off. */
        }
        cancel_center();
        if (cur < 0) return STOP;
        st.touch_mode = 0;
        select(&g_menu, g_menu.id[cur]);
        widget_invalidate_force(w, (void *)0);
        st.last_center = now;
        st.center_top = top;
        st.center_surface = w;
        st.center_scope = g_menu.scope;
        st.center_ctx = g_menu.ctx;
        st.center_rows = g_menu.rows;
        st.center_id = g_menu.id[cur];
        row_id_t r = row_id(g_menu.at[cur]);
        st.center_hash = r.one;
        st.center_hash2 = r.two;
        st.center_timer = timer_add(confirm_center, (void *)0, DOUBLE_CLICK_MS);
        st.center_token = st.center_timer;
        if (st.center_timer) {
            prop(top, CONFIRM, (int)st.center_token);
            prop(w, CONFIRM, (int)st.center_token);
            if (g_menu.kind != 2) prop(g_menu.at[cur], CONFIRM, (int)st.center_token);
        } else
            cancel_center(); /* Allocation failure consumes the press without a click. */
        return STOP;
    }
    st.touch_mode = 0;
    if (is_home(top, w)) {
        home_step(w, dir, now);
        widget_invalidate_force(w, (void *)0);
        return STOP;
    }
    /* Every accepted tick times the run and takes its place in it, short lists included. */
    int list = IPOD && g_menu.n; /* iPod row lists: the gentler ramp */
    int step = ramp(top, g_menu.w, g_menu.scope, g_menu.ctx, dir, now,
                    list ? LIST_FIRST_MS : WHEEL_RAMP_MS, list ? LIST_RAMP_MS : WHEEL_RAMP_MS);
    if (g_menu.rows <= SHORT_LIST_MAX) {
        st.wheel_run = 0;
        step = 1;
    }
    native_scrollbar(&g_menu);
    if (g_menu.kind == 3) {
        if (dir > 0)
            slide_menu_scroll_to_next(w);
        else
            slide_menu_scroll_to_prev(w);
    } else if (g_menu.n) {
        int id = widget_get_prop_int(w, SEL, -1);
#if IPOD
        /* The overshoot tick (ramp) only wakes the list. Revealing the selection and a turn against
         * an end are never dropped, so every tick there bumps and the wrap pause is timed between
         * ticks. */
        if (id >= 0 && st.wheel_tick == 2 && clamp_step(id, g_menu.rows - 1, dir) != id) {
            stop_scroll(&g_menu);
            widget_invalidate_force(w, (void *)0);
            return STOP;
        }
        if (step > 1) rearm(&st.letter_timer, letter_expire, LETTER_MS);
#endif
        int next = clamp_step(id < 0 ? (cur >= 0 ? g_menu.id[cur] : 0) : id, g_menu.rows - 1,
                              id < 0 ? 0 : dir * step);
        if (next == 0 || next == g_menu.rows - 1) drop_wheel();
        if (next == id) {
            if (!ring_list(&g_menu) || id < 0) {
                stop_scroll(&g_menu);
                widget_invalidate_force(w, (void *)0); /* a wheel wake still clears touch hiding */
                return STOP;
            }
            if (!edge_wraps(&g_menu, id, dir, now)) {
                st.edge_id = id;
                st.edge_dir = dir;
                st.edge_time = now;
                fx_arm(w, dir);
                stop_scroll(&g_menu);
                widget_invalidate_force(w, (void *)0);
                return STOP;
            }
            /* A detent after a pause at the bumped end: carry over to the other end of this list.
             */
            st.bump_dir = 0;
            next = dir > 0 ? 0 : g_menu.rows - 1;
        }
        st.edge_id = -1; /* left the end: the next boundary detent bumps again */
        select(&g_menu, next);
        /* Stop momentum even when the selected row already fits the viewport. */
        reveal(&g_menu, next, 1);
#if IPOD
        if (owned && id >= 0) buzzeer_switch(1); /* one click per row change, however far */
#endif
    } else {
        int top = view_top(&g_menu);
        int next = clamp_step(top, max_top(&g_menu), dir * RING_STEP * step);
        if (next == 0 || next >= max_top(&g_menu)) st.wheel_run = 0;
        wheel_offset(&g_menu, next);
    }
    widget_invalidate_force(w, (void *)0);
    return STOP;
}
