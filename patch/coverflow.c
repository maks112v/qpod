/* Coverflow (docs/internals.md): the stock Local Music albums on a stock slide_menu, the way
 * PictureFlow reads Rockbox's database. No tag reads: albums and tracks come from the stock library
 * queries, and the only thing Coverflow owns is a thumbnail cache, built on a modal screen by one
 * pthread that touches only files, the two stock art locks and the volatile counters below. */
#include "offsets.inc"
#include "peq.h"
#ifndef PEQ_HOST
#include "stock.h"
#endif
#include "coverflow_render.inc"

/* On the card, beside stock's own cover cache (/mnt/mmc/.sldp). */
#define ART_DIR PEQ_ROOT "/mnt/mmc/.coverflow"
#define LAST_ALBUM ART_DIR "/album" /* the centre album's key, so a reboot opens on it */
#if IPOD /* iPod insets its text from the rounded glass (offsets.inc CF_*); Stock keeps its layout */
#define CF_X CF_EDGE
#define CF_W (375 - 2 * CF_EDGE)
#define CF_ROW_X CF_X /* one text column throughout */
#define CF_ROW_W CF_W
#else
#define CF_X 8
#define CF_W 359
#define CF_ROW_X 12
#define CF_ROW_W 350
#endif
#define MIN_FREE_MB 16 /* no new cache files below this much free space on the card */
#define ART_NEAR 3 /* real art only this many covers either side, like PictureFlow's cache */
#define PLACEHOLDER "default_album_big"
#define CARDS 2 /* after the albums: Sort, then Refresh library */

extern int stock_home_trampoline(void *win, void *ctx), stock_scan_all_trampoline(void *, void *),
    stock_scan_folder_trampoline(void *, void *), stock_delete_song_trampoline(void *, void *);

enum { PREPARING, COVERS, TRACKS };
typedef struct {
    char *track; /* the album's first track, copied so the thread never reads stock deques */
    unsigned key;
} job_t;
static struct {
    void *page, *body, *covers, *slide, *name, *artist, *title;
    void *albums, *tracks; /* our copies of the stock query results; albums in the Sort's order */
    void *stock;           /* the albums in stock order, which albums is, or is sorted from */
    int sort, sort_read;   /* the Sort card's order (SORT_*), from SORT_FILE once */
    int on_sort;           /* the next load centres the Sort card */
    job_t *jobs;
    unsigned long thread;
    unsigned timer;
    unsigned saved_album, albums_gen; /* albums_gen: library_gen when albums was queried */
    int screen, album, running;
    volatile int done, total, cancel;
} cf __attribute__((section(".scratch")));

static int pick(void *ctx, void *event);

/* The album list is kept across opens until songtable changes. Only these three stock functions
 * write it; each moves the generation before and after it runs, so a list queried meanwhile is
 * never kept. The scans run on stock's scan thread. */
volatile unsigned library_gen __attribute__((section(".scratch"))); /* also Most Played's */
static int library_write(int (*stock)(void *, void *), void *a, void *b) {
    ++library_gen;
    int result = stock(a, b);
    ++library_gen;
    return result;
}
int coverflow_scan_all(void *a, void *b) { return library_write(stock_scan_all_trampoline, a, b); }
int coverflow_scan_folder(void *a, void *b) { return library_write(stock_scan_folder_trampoline, a, b); }
int coverflow_delete_song(void *a, void *b) { return library_write(stock_delete_song_trampoline, a, b); }

/* The queue menu resolves the live track list again after its dialog closes. */
void *coverflow_tracks(void *page) {
    return page == cf.page && cf.screen == TRACKS ? cf.tracks : 0;
}

/* Only a live album card, never Sort, Refresh or a pending screen change. */
void *coverflow_album(void *page) {
    if (page != cf.page || cf.screen != COVERS || !cf.slide || cf.timer) return 0;
    unsigned i = (unsigned)I(cf.slide, SLIDE_INDEX);
    return cf.albums && i < deque_size(cf.albums) ? deque_at(cf.albums, i) : 0;
}

/* FNV-1a, shared with navigation.c */
unsigned hash_bytes(unsigned h, const unsigned char *s, unsigned n) {
    for (unsigned i = 0; i < n; ++i) h = (h ^ s[i]) * 16777619u;
    return h;
}

unsigned fnv(unsigned h, const unsigned char *s) {
    while (s && *s) h = hash_bytes(h, s++, 1);
    return h * 16777619u; /* a separator, so "ab"+"c" and "a"+"bc" differ */
}

static unsigned tags_key(const char *artist, const char *album) {
    return fnv(fnv(FNV_SEED, (const unsigned char *)artist), (const unsigned char *)album);
}

static unsigned album_key(void *r) { return tags_key(P(r, REC_ARTIST), P(r, REC_ALBUM)); }

/* Track selection uses ringnav's existing position memory, keyed by this album. */
unsigned coverflow_scope(void *page) {
    return coverflow_tracks(page) ? album_key(deque_at(cf.albums, (unsigned)cf.album)) : 0;
}

/* ART_DIR/<key>.jpg<suffix>; out holds at least 512 bytes. */
static char *art_path(char *out, unsigned key, const char *suffix) {
    tk_snprintf(out, 512, ART_DIR "/%08x.jpg%s", key, suffix);
    return out;
}

/* stock's thumbnailer, under the lock every stock caller holds; shared with photos.c */
int thumb(const char *src, const char *dst, int w, int h) {
    pthread_mutex_lock((void *)parse_cover_mutex);
    int ok = toolsThumbSpecCover(src, dst, w, h) == 1;
    pthread_mutex_unlock((void *)parse_cover_mutex);
    return ok;
}

/* One album: cover.jpg, folder.jpg, then embedded art, written to .tmp and renamed. An empty
 * file marks "no art", so the placeholder shows and the album is not retried until Refresh. */
static void build_art(const job_t *j) {
    char dst[512], tmp[512], src[1024];
    if (access(j->track, 0)) return; /* storage gone: no marker, the next open retries */
    art_path(dst, j->key, "");
    art_path(tmp, j->key, ".tmp");
    const char *slash = strrchr(j->track, '/');
    int folder = slash ? (int)(slash - j->track) : 0, ok = 0;
    static const char *const names[] = { "cover.jpg", "folder.jpg" };
    for (int i = 0; i < 2 && !ok; ++i) {
        snprintf(src, sizeof(src), "%.*s/%s", folder, j->track, names[i]);
        ok = !access(src, 4) && thumb(src, tmp, ART_SIZE, ART_SIZE);
    }
    if (!ok) {
        /* toolsGetAlbumCover goes through the shared /tmp/.tmp_picture, which stock guards with
         * either lock (parse_albumcovertask_thd, player_parsecover_thd); hold both. Stock never
         * nests them, so this order cannot deadlock. */
        pthread_mutex_lock((void *)parse_cover_mutex);
        pthread_mutex_lock((void *)g_playcover_mutex);
        ok = toolsGetAlbumCover(j->track, tmp, ART_SIZE, ART_SIZE) == 1;
        pthread_mutex_unlock((void *)g_playcover_mutex);
        pthread_mutex_unlock((void *)parse_cover_mutex);
    }
    if (ok && !rename(tmp, dst)) return;
    unlink(tmp);
    if (access(j->track, 0)) return;
    void *f = fopen(dst, "w");
    if (f) fclose(f);
}

static void *worker(void *unused) {
    (void)unused;
    while (cf.done < cf.total && !cf.cancel) {
        build_art(&cf.jobs[cf.done]);
        ++cf.done;
    }
    return 0;
}

/* A running worker cancelled and joined, and its poll timer stopped; 1 when one ran. Shared with
 * photos.c and books.c. */
int worker_stop(unsigned long thread, int *running, volatile int *cancel, unsigned *timer) {
    int ran = *running;
    if (ran) {
        *cancel = 1;
        pthread_join(thread, 0);
        *running = 0;
    }
    stop_timer(timer);
    return ran;
}

/* Whether dir's file system has MIN_FREE_MB free (statfs, MIPS o32 layout: f_bsize is word 1,
 * f_bavail word 7); shared with photos.c. */
int card_space(const char *dir) {
    unsigned fs[32] = { 0 };
    return !statfs(dir, fs) && (unsigned long long)fs[7] * fs[1] >= (unsigned long long)MIN_FREE_MB << 20;
}

/* Cancel stops after the current album; finished thumbnails stay, so the next open resumes. */
static void stop(void) {
    worker_stop(cf.thread, &cf.running, &cf.cancel, &cf.timer);
    for (int i = 0; i < cf.total; ++i) free(cf.jobs[i].track);
    free(cf.jobs);
    cf.jobs = 0;
    cf.total = cf.done = 0;
}

static void fx_close(void);

static void drop(void) {
    stop();
    fx_close();
    if (cf.tracks) deque_destroy(cf.tracks);
    cf.tracks = 0;
}

static void drop_albums(void) {
    if (cf.albums && cf.albums != cf.stock) deque_destroy(cf.albums);
    if (cf.stock) deque_destroy(cf.stock);
    cf.albums = cf.stock = 0;
}

/* A stock library query's rows (*count its result) copied out of the staging deque, which is
 * restored so the query leaves no trace; shared with navigation.c's queue menu. */
void *staged(int (*query)(void *), void *arg, int *count) {
    void *dir = P(tools_pdeq_directory, 0), *save = _create_deque("stSongInfo"),
         *out = _create_deque("stSongInfo");
    deque_init_copy(save, dir);
    *count = query(arg);
    deque_init_copy(out, dir);
    deque_clear(dir);
    deque_assign(dir, save);
    deque_destroy(save);
    return out;
}

/* getAllAlbum, as load_localclass_list 0xf003 runs it, or the album's getMusicByAlbum, as its row
 * opens it. Stock order, including the trailing "Unknown Album" (id -1) row. */
static int albums(void *album) {
    return album ? getMusicByAlbum(I(album, REC_ID) == -1 ? (const char *)0 : P(album, REC_ALBUM))
                 : getAllAlbum();
}

/* getAllAlbum ends with an "Unknown Album" row (id -1) whenever any album exists, even when every
 * song has an album tag. It goes when the query its card opens, getMusicByAlbum(NULL), finds no
 * song, as navigation.c's ringnav_localclass drops it from the Albums list. */
static void drop_unknown(void) {
    unsigned n = deque_size(cf.stock);
    void *last = n ? deque_at(cf.stock, n - 1) : 0;
    if (!last || I(last, REC_ID) != -1) return;
    int found;
    void *songs = staged(albums, last, &found);
    if (!deque_size(songs)) deque_pop_back(cf.stock);
    deque_destroy(songs);
}

/* Sort, the card before Refresh (docs/internals.md#coverflow-sort): Album is the stock order;
 * Artist sorts by artist without a leading article, as every library list does (ringnav_sort_key),
 * then year, then album; Recently Added by the newest song's time_create; Most Played by the
 * album's listens (navigation.c album_plays). Ties keep the stock order, and the Unknown row stays
 * last. Ranks come from getAllAlbum's own row callback over the same grouping with another ORDER BY
 * (toolsQueryDbTable without its name sort), matched back to the stock rows by album name, case
 * aside, as the grouping is. */
enum { SORT_ALBUM, SORT_ARTIST, SORT_ADDED, SORT_PLAYED, SORT_N };
#define SORT_FILE PEQ_ROOT "/mnt/data/ringnav-coversort"
#define SORT_SQL "select id,album,songer,fileurl from songtable group by album COLLATE NOCASE order by "
static const char *const sort_names[SORT_N] = { "Sort: Album", "Sort: Artist", "Sort: Recently Added",
                                                "Sort: Most Played" };
extern void album_plays(int (*album_of)(void *), unsigned *sum);
extern void ringnav_sort_key(char *s);

typedef struct {
    unsigned key;
    int idx;
} name_t;
typedef struct {
    void *r;
    const char *artist; /* Artist: the artist's sort key, without its article */
    unsigned rank;
    int idx;
} order_t;
#define ARTIST_KEY 96
static struct {
    name_t *names; /* the stock rows by name_key, the Unknown row left out, for album_index */
    unsigned n;
} by_name __attribute__((section(".scratch")));

static const char *album_name(void *r) {
    const char *s = P(r, REC_ALBUM);
    return s ? s : "";
}
static unsigned name_key(void *r) { /* the album name, ASCII case aside */
    unsigned h = FNV_SEED;
    for (const unsigned char *s = (const unsigned char *)album_name(r); *s; ++s) {
        unsigned char c = *s >= 'A' && *s <= 'Z' ? *s + 32 : *s;
        h = hash_bytes(h, &c, 1);
    }
    return h;
}
static int by_key(const void *a, const void *b) {
    const name_t *x = a, *y = b;
    return x->key != y->key ? (x->key < y->key ? -1 : 1) : x->idx - y->idx;
}
/* The stock row whose album r names, case aside, or -1: the hash finds the run, the names decide. */
static int album_index(void *r) {
    unsigned key = name_key(r), lo = 0, hi = by_name.n;
    while (lo < hi) {
        unsigned mid = (lo + hi) / 2;
        if (by_name.names[mid].key < key) lo = mid + 1;
        else hi = mid;
    }
    for (; lo < by_name.n && by_name.names[lo].key == key; ++lo) {
        int i = by_name.names[lo].idx;
        if (!strcasecmp(album_name(deque_at(cf.stock, (unsigned)i)), album_name(r))) return i;
    }
    return -1;
}
static int rank_query(void *sql) { return toolsQueryDbTable("/mnt/data/database.db", sql, album_row, 0); }
static void rank_by(order_t *v, const char *sql) {
    int n;
    void *rows = staged(rank_query, (void *)sql, &n);
    for (unsigned j = 0; j < deque_size(rows); ++j) {
        int i = album_index(deque_at(rows, j));
        if (i >= 0 && v[i].rank == ~0u) v[i].rank = j;
    }
    deque_destroy(rows);
}
static int by_rank(const void *a, const void *b) {
    const order_t *x = a, *y = b;
    int unknown = (I(x->r, REC_ID) == -1) - (I(y->r, REC_ID) == -1); /* the Unknown card last */
    if (unknown) return unknown;
    return x->rank != y->rank ? (x->rank < y->rank ? -1 : 1) : x->idx - y->idx;
}
static int by_artist(const void *a, const void *b) {
    const order_t *x = a, *y = b;
    int d = (I(x->r, REC_ID) == -1) - (I(y->r, REC_ID) == -1);
    if (!d) d = strcasecmp(x->artist, y->artist);
    return d ? d : by_rank(a, b);
}

/* cf.stock in the Sort's order: cf.stock itself for Album. Out of memory, the Sort falls back to
 * Album too, so its card says what is shown. */
static void *sorted(void) {
    unsigned n = deque_size(cf.stock), m = 0;
    int artist = cf.sort == SORT_ARTIST, played = cf.sort == SORT_PLAYED;
    order_t *v = cf.sort == SORT_ALBUM ? 0 : calloc(n + 1, sizeof *v);
    name_t *names = v ? calloc(n + 1, sizeof *names) : 0;
    unsigned *sum = names && played ? calloc(n + 1, sizeof *sum) : 0;
    char *keys = names && artist ? calloc(n + 1, ARTIST_KEY) : 0;
    if (!names || (played && !sum) || (artist && !keys)) {
        free(v), free(names), free(sum), free(keys);
        cf.sort = SORT_ALBUM;
        return cf.stock;
    }
    for (unsigned i = 0; i < n; ++i) {
        void *r = v[i].r = deque_at(cf.stock, i);
        v[i].rank = ~0u, v[i].idx = (int)i;
        if (I(r, REC_ID) != -1) names[m].key = name_key(r), names[m++].idx = (int)i;
        if (keys) {
            const char *a = P(r, REC_ARTIST);
            snprintf(keys + i * ARTIST_KEY, ARTIST_KEY, "%s", a ? a : "");
            ringnav_sort_key(keys + i * ARTIST_KEY);
            v[i].artist = keys + i * ARTIST_KEY;
        }
    }
    qsort(names, m, sizeof *names, by_key);
    by_name.names = names, by_name.n = m;
    if (sum) {
        album_plays(album_index, sum);
        for (unsigned i = 0; i < n; ++i)
            if (sum[i]) v[i].rank = ~sum[i]; /* most first; never played after, in stock order */
    } else
        rank_by(v, cf.sort == SORT_ADDED ? SORT_SQL "max(time_create) desc"
                                         : SORT_SQL "ifnull(max(year),0)=0,max(year),album COLLATE NOCASE");
    by_name.names = 0, by_name.n = 0;
    qsort(v, n, sizeof *v, artist ? by_artist : by_rank);
    void *out = _create_deque("stSongInfo");
    deque_init(out);
    for (unsigned i = 0; i < n; ++i) _deque_push_back(out, v[i].r);
    free(v), free(names), free(sum), free(keys);
    return out;
}

void *text(void *parent, int x, int y, int w, int h) { /* shared with photos.c */
    void *label = hscroll_label_create(parent, x, y, w, h);
    widget_use_style(label, "s_scrlabel_white20c");
    set_hscroll_label_attribute(label);
    widget_set_prop_int(label, "loop", 1);
    return label;
}

/* Stock's curly quotes have a full-width advance, leaving a gap inside words. Use their
 * compact ASCII forms in labels only; library records and scrobble tags stay untouched. */
static void display_text(void *label, const char *caption) {
    if (!caption) caption = "";
    const unsigned char *s = (const unsigned char *)caption;
    while (*s && *s != 0xe2) ++s;
    if (!*s) {
        widget_set_text_utf8(label, caption);
        return;
    }
    char *buf = calloc(strlen(caption) + 1, 1);
    if (!buf) {
        widget_set_text_utf8(label, caption);
        return;
    }
    s = (const unsigned char *)caption;
    char *out = buf;
    while (*s) {
        if (s[0] == 0xe2 && s[1] == 0x80 &&
            (s[2] == 0x98 || s[2] == 0x99 || s[2] == 0x9c || s[2] == 0x9d)) {
            *out++ = s[2] < 0x9c ? '\'' : '"';
            s += 3;
        } else
            *out++ = (char)*s++;
    }
    *out = 0;
    widget_set_text_utf8(label, buf);
    free(buf);
}

/* A black page named name, with its destroy and Return handlers; 0 when none. Shared with photos.c
 * and books.c. */
void *page_open(const char *name, int (*closed)(void *, void *), int (*keyup)(void *, void *)) {
    void *page = window_create(0, 0, 0, 0, 0);
    if (!page) return 0;
    widget_set_name(page, name);
    widget_set_prop_int(page, "style:normal:bg_color", (int)0xff000000u);
    widget_on(page, EVT_DESTROY, closed, 0);
    widget_on(page, EVT_KEY_UP, keyup, 0);
    return page;
}

/* A page's 48px title bar; shared with photos.c. */
void *page_title(void *body, const char *caption) {
    void *title = text(body, CF_X, 0, CF_W, 48);
    display_text(title, caption);
    return title;
}

/* The peq_ui.c page: a title bar (*title, unless 0) over n whole item_h rows in body, shrunk so the list's
 * white background never shows below a short list. Shared with photos.c. */
void *page_list(void *page, void *body, void **title, const char *caption, int n, int item_h) {
    int h = widget_get_prop_int(page, "h", 290), rows = (h - 48) / item_h * item_h;
    if (n * item_h < rows) rows = n * item_h;
    widget_destroy_children(body);
    widget_set_visible(body, 1, 0);
    void *t = page_title(body, caption);
    if (title) *title = t;
    void *lv = list_view_create(body, 0, 48, 375, rows);
    widget_set_prop_int(lv, "item_height", item_h);
    /* The theme's default list_view is a light card; stock pages paint theirs black inline. */
    widget_set_prop_int(lv, "style:normal:bg_color", (int)0xff000000u);
    widget_set_prop_int(lv, "style:normal:border_color", 0);
    void *view = scroll_view_create(lv, 0, 0, 375, rows);
    widget_set_prop_int(view, "yslidable", 1);
    widget_set_prop_int(view, "xslidable", 0);
    widget_set_prop_int(view, "virtual_h", n * item_h);
    widget_invalidate_force(page, 0);
    return view;
}

static void *list(const char *title, int n) {
    return page_list(cf.page, cf.body, &cf.title, title, n, 48);
}

/* One 48px row of a page_list or, with a detail (navigation.c's Most Played), a 64px one: the caption
 * over the detail in 16px #AAAAAA, stock's s_scrlabel_gray24l grey. Shared with photos.c. */
void page_row_detail(void *view, int index, const char *caption, const char *detail,
                     int (*click)(void *, void *)) {
    int h = detail ? 64 : 48;
    void *item = list_item_create(view, 0, index * h, 375, h);
    widget_use_style(item, "s_listitem_black");
    void *label = text(item, CF_ROW_X, detail ? 4 : 0, CF_ROW_W, detail ? 32 : 48);
    display_text(label, caption);
    if (detail) {
        label = text(item, CF_ROW_X, 36, CF_ROW_W, 24);
        widget_set_prop_int(label, "style:normal:text_color", (int)0xffaaaaaau);
        widget_set_prop_int(label, "style:normal:font_size", 16);
        display_text(label, detail);
    }
    widget_on(item, EVT_CLICK, click, (void *)(long)index);
}

/* clip: the canvas clip, read into old, narrowed to x, y, w, h; false when nothing shows. Shared
 * with navigation.c. */
int clip_within(void *canvas, int *old, int *clip, int x, int y, int w, int h) {
    canvas_get_clip_rect(canvas, old);
    clip[0] = old[0] > x ? old[0] : x;
    clip[1] = old[1] > y ? old[1] : y;
    clip[2] = (old[0] + old[2] < x + w ? old[0] + old[2] : x + w) - clip[0];
    clip[3] = (old[1] + old[3] < y + h ? old[1] + old[3] : y + h) - clip[1];
    if (clip[2] < 0) clip[2] = 0;
    if (clip[3] < 0) clip[3] = 0;
    return clip[2] && clip[3];
}

/* Resume, play counts, Books' pages and the last album: each file is written whole, to a .tmp then
 * renamed. Shared with navigation.c and books.c. */
void blob_io(const char *path, const char *tmp, void *buf, unsigned size, int write) {
    void *f = fopen(write ? tmp : path, write ? "wb" : "rb");
    if (!f) return;
    int ok = write ? fwrite(buf, size, 1, f) == 1 : fread(buf, size, 1, f) == 1;
    if (fclose(f) || !ok) {
        if (!write) memset(buf, 0, size);
        return;
    }
    if (write) rename(tmp, path);
}

/* Stock pattern (album rows, Now Playing): load the file, set it, drop the load's reference, so the
 * next paint decodes the file again rather than a stale cached copy. Returns 0 when the load fails,
 * as for the empty "no art" marker. size, if given, gets the image's width and height (bitmap_t
 * w @0, h @4). */
static int show(void *img, const char *url, unsigned *size) {
    unsigned bitmap[64]; /* bitmap_t */
    if (widget_load_image(img, url, bitmap)) return 0;
    if (size) size[0] = bitmap[0], size[1] = bitmap[1];
    image_base_set_image(img, url);
    widget_unload_image(img, bitmap);
    return 1;
}

/* The depth renderer's state: the frame, the textures of the covers around the visual position
 * and the placeholder's. No frame: the flat fallback (stock images on the slide_menu). */
static struct {
    void *frame;                  /* bitmap_t *, CF_VIEW_W x CF_VIEW_H RGBA8888 */
    unsigned *tex;                /* CF_RING texture slots, then the placeholder */
    int album[CF_RING];           /* each slot's album + 1; 0 is empty */
    const unsigned *art[CF_RING]; /* its texture: the slot's own, or the placeholder */
    int c, frac, drawn;           /* the position the frame holds, once drawn */
} fx __attribute__((section(".scratch")));

/* A decoded image copied into a texture: its centred square, nearest sampled to ART_SIZE, over
 * black. Only 32-bit formats (BITMAP_RGBA_AT); stock decodes covers to RGBA8888 with straight
 * alpha. The image manager's copy is only
 * read, and a cover's load is dropped again at once, as stock does. */
static int decode(const char *url, unsigned *out, int unload) {
    static const unsigned char at[4][4] = BITMAP_RGBA_AT;
    unsigned bitmap[64]; /* bitmap_t */
    if (widget_load_image(cf.page, url, bitmap)) return 0;
    unsigned w = bitmap[0], h = bitmap[1], format = ((unsigned short *)bitmap)[7] - 1u;
    const unsigned char *data =
        format < 4 && w && h && w <= 4096 && h <= 4096 ? bitmap_lock_buffer_for_read(bitmap) : 0;
    if (data) {
        const unsigned char *o = at[format];
        unsigned stride = bitmap_get_line_length(bitmap), side = w < h ? w : h, xoff[ART_SIZE];
        const unsigned char *base = data + (h - side) / 2 * stride + (w - side) / 2 * 4;
        for (unsigned x = 0; x < ART_SIZE; ++x) xoff[x] = x * side / ART_SIZE * 4;
        for (unsigned y = 0; y < ART_SIZE; ++y) {
            const unsigned char *line = base + y * side / ART_SIZE * stride;
            for (unsigned x = 0; x < ART_SIZE; ++x) {
                const unsigned char *px = line + xoff[x];
                unsigned a = px[o[3]], r = px[o[0]], g = px[o[1]], b = px[o[2]];
                if (a < 255) r = r * a / 255, g = g * a / 255, b = b * a / 255; /* over black */
                *out++ = 0xff000000u | r | g << 8 | b << 16;
            }
        }
        bitmap_unlock_buffer(bitmap);
    }
    if (unload) widget_unload_image(cf.page, bitmap);
    return data != 0;
}

static void fx_close(void) {
    if (fx.frame) bitmap_destroy(fx.frame);
    free(fx.tex);
    memset(&fx, 0, sizeof(fx));
}

/* The frame and every texture at once, or neither: the flat covers then stand in. */
static int fx_open(void) {
    fx.frame = bitmap_create_ex(CF_VIEW_W, CF_VIEW_H, CF_VIEW_W * 4, 1 /* RGBA8888 */);
    fx.tex = fx.frame ? calloc(CF_RING + 1, CF_TEXELS * 4) : 0;
    if (!fx.tex) {
        fx_close();
        return 0;
    }
    *(unsigned short *)((char *)fx.frame + 0xc) |= 1; /* BITMAP_FLAG_OPAQUE: every pixel is */
    unsigned *placeholder = fx.tex + CF_RING * CF_TEXELS;
    if (!decode(PLACEHOLDER, placeholder, 0)) /* a theme image: the manager keeps it */
        for (int i = 0; i < CF_TEXELS; ++i) placeholder[i] = 0xff3a3a3au;
    return 1;
}

/* Album a's cached art in buf, else the placeholder (no art, or the Refresh card). */
static const unsigned *fx_load(int a, unsigned *buf) {
    char url[600] = "file://";
    if ((unsigned)a < deque_size(cf.albums)) {
        art_path(url + 7, album_key(deque_at(cf.albums, (unsigned)a)), "");
        if (decode(url, buf, 1)) return buf;
    }
    return fx.tex + CF_RING * CF_TEXELS;
}

/* The CF_RING covers around album c of n stay decoded, others are dropped as movement crosses
 * albums; ring gets each ring slot's texture. A small library repeats albums round the ring, as
 * the slide_menu wraps. */
static void fx_window(int c, int n, const unsigned *ring[CF_RING]) {
    int want[CF_RING];
    for (int j = 0; j < CF_RING; ++j) want[j] = ((c + j - CF_REACH) % n + n) % n + 1;
    for (int i = 0; i < CF_RING; ++i) {
        int keep = 0;
        for (int j = 0; j < CF_RING; ++j) keep |= fx.album[i] == want[j];
        if (!keep) fx.album[i] = 0;
    }
    for (int j = 0; j < CF_RING; ++j) {
        int i = 0;
        while (i < CF_RING && fx.album[i] != want[j]) ++i;
        if (i == CF_RING) { /* at most CF_RING albums are wanted, so a slot is free */
            for (i = 0; fx.album[i]; ++i) {}
            fx.album[i] = want[j];
            fx.art[i] = fx_load(want[j] - 1, fx.tex + i * CF_TEXELS);
            fx.drawn = 0;
        }
        ring[j] = fx.art[i];
    }
}

static int floor_div(int a, int b) { /* floor division, b > 0 */
    return a / b - (a % b < 0);
}

/* The visual position from the slide_menu's index and live offset (the wheel's animator): album c
 * (of n) at the centre and frac past it. Stock completion commits index - offset / stride. */
int visual(void *s, int n, int *c, int *frac) { /* shared with photos.c */
    int stride = slide_menu_item_width(s) + I(s, SLIDE_SPACER), d = -I(s, SLIDE_OFFSET);
    if (n <= 0 || stride <= 0) return 0;
    int q = floor_div(2 * d + stride, 2 * stride);
    *frac = (d - q * stride) * CF_ONE / stride;
    *c = ((I(s, SLIDE_INDEX) + q) % n + n) % n;
    return stride;
}

/* ringnav_paint (the border hook, after stock painted the slide_menu's empty children) calls this
 * for every widget: over Coverflow's slide_menu it draws the frame, rendered again only when the
 * position or a texture has changed. */
void coverflow_paint(void *w, void *canvas) {
    int c, frac, n;
    if (!w || w != cf.slide || !fx.frame || cf.screen != COVERS ||
        !visual(w, n = (int)widget_count_children(w), &c, &frac))
        return;
    const unsigned *ring[CF_RING];
    fx_window(c, n, ring);
    if (!fx.drawn || c != fx.c || frac != fx.frac) {
        unsigned *d = (unsigned *)bitmap_lock_buffer_for_write(fx.frame);
        if (!d) return;
        coverflow_render(d, (int)(bitmap_get_line_length(fx.frame) / 4), frac, ring);
        bitmap_unlock_buffer(fx.frame);
        fx.c = c, fx.frac = frac, fx.drawn = 1;
    }
    int r[4] = { 0, 0, CF_VIEW_W, CF_VIEW_H };
    canvas_draw_image(canvas, fx.frame, r, r);
}

static void cover(void *img, unsigned i, int near) {
    char url[600] = "file://";
    near = near && i < deque_size(cf.albums);
    if (near) art_path(url + 7, album_key(deque_at(cf.albums, i)), "");
    if (tk_strcmp(widget_get_prop_str(img, "image", ""), near ? url : PLACEHOLDER) &&
        (!near || !show(img, url, 0)))
        image_base_set_image(img, PLACEHOLDER);
}

static int changed(void *ctx, void *event) {
    (void)ctx;
    (void)event;
    unsigned n = widget_count_children(cf.slide), c = (unsigned)I(cf.slide, SLIDE_INDEX);
    for (unsigned i = 0; !fx.frame && i < n; ++i) { /* the flat fallback's own images */
        unsigned d = i > c ? i - c : c - i;
        cover(widget_get_child(cf.slide, i), i, d <= ART_NEAR || n - d <= ART_NEAR);
    }
    void *r = c + CARDS < n ? deque_at(cf.albums, c) : (void *)0;
    if (r) cf.saved_album = album_key(r);
    display_text(cf.name, r ? P(r, REC_ALBUM) : c + 1 < n ? sort_names[cf.sort] : "Refresh library");
    display_text(cf.artist, r ? P(r, REC_ARTIST) : "");
    return 0;
}

/* One child per album plus the Sort and Refresh cards, moved by the wheel only: the slide_menu takes no
 * touch. With depth it spans the frame, so every step repaints all of it, and its CF_VIEW_H square
 * items with a negative spacer move one album per CF_STRIDE px; the children stay empty under the
 * frame. The flat fallback is the stock images, 160 px, as before. ponytail: one child per album;
 * if large libraries lag on hardware, virtualize to a recycled window of children. */
static void covers(void) {
    cf.screen = COVERS;
    widget_set_visible(cf.body, 0, 0);
    if (!cf.covers) {
        void *f = widget_factory();
        int depth = fx_open();
        cf.covers = widget_factory_create_widget(f, "view", cf.page, 0, 0, 375, 290);
        cf.slide = widget_factory_create_widget(f, "slide_menu", cf.covers, 0, depth ? 0 : 24, 375,
                                                depth ? CF_VIEW_H : ART_SIZE);
        if (depth) slide_menu_set_spacer(cf.slide, CF_STRIDE - CF_VIEW_H);
        widget_set_sensitive(cf.slide, 0);
        for (unsigned i = 0, n = deque_size(cf.albums); i < n + CARDS; ++i) {
            void *img = image_create(cf.slide, 0, 0, 0, 0);
            image_set_draw_type(img, 4); /* scale_auto, as the stock cover rows */
            if (!depth) image_base_set_image(img, PLACEHOLDER);
            widget_set_prop_int(img, "clickable", 1);
            widget_on(img, EVT_CLICK, pick, (void *)(long)i);
        }
        /* Album over artist under the frame, white and larger, then grey, clear of the rounded
         * glass (docs/ipod.md#coverflow); the same in both builds. */
        cf.name = text(cf.covers, CF_EDGE, CF_TEXT_Y, 375 - 2 * CF_EDGE, CF_NAME_H);
        widget_set_prop_int(cf.name, "style:normal:font_size", CF_NAME_PX);
        cf.artist = text(cf.covers, CF_EDGE, CF_TEXT_Y + CF_NAME_H, 375 - 2 * CF_EDGE, CF_ARTIST_H);
        widget_set_prop_int(cf.artist, "style:normal:font_size", CF_ARTIST_PX);
        widget_set_prop_int(cf.artist, "style:normal:text_color", (int)CF_GREY);
        slide_menu_set_value(cf.slide, cf.album);
        widget_on(cf.slide, EVT_VALUE_CHANGED, changed, 0);
        changed(0, 0);
    }
    widget_set_visible(cf.covers, 1, 0);
    widget_invalidate_force(cf.page, 0);
}

static int to_covers(const void *unused) {
    (void)unused;
    cf.timer = 0;
    stop();
    covers();
    return 0;
}

static int cancel_row(void *ctx, void *event) {
    (void)ctx;
    (void)event;
    rearm(&cf.timer, to_covers, 0);
    return 0;
}

static int poll(const void *unused) {
    (void)unused;
    cf.timer = 0;
    if (cf.done == cf.total) return to_covers(0); /* stop() joins the thread's last steps */
    char progress[64];
    tk_snprintf(progress, sizeof(progress), "Preparing artwork\xe2\x80\xa6 %d/%d", cf.done,
                cf.total);
    widget_set_text_utf8(cf.title, progress);
    cf.timer = timer_add(poll, 0, 250);
    return 0;
}

/* On open and Refresh: the albums, then art for the ones with no cache file (PictureFlow's
 * first-launch build; later opens resume). The albums are queried again only on Refresh or after
 * the library changed: stock's sort converts both names to pinyin on every comparison. */
static void load(void) {
    int on_sort = cf.on_sort; /* taken whatever this load shows */
    cf.on_sort = 0;
    drop();
    widget_destroy_children(cf.page);
    cf.covers = cf.slide = cf.name = cf.artist = 0; /* destroyed with the page's children */
    cf.album = 0;
    cf.body = widget_factory_create_widget(widget_factory(), "view", cf.page, 0, 0, 375, 290);
    int n = 0;
    unsigned gen = library_gen;
    if (cf.albums_gen != gen) drop_albums();
    if (cf.stock)
        n = (int)deque_size(cf.stock);
    else if (!*(volatile int *)SCAN_THREAD || *(volatile int *)SCAN_DONE) {
        cf.stock = staged(albums, 0, &n);
        cf.albums_gen = gen;
        drop_unknown();
    }
    if (n > 0 && !cf.albums) {
        if (!cf.sort_read) BLOB_IO(SORT_FILE, cf.sort, 0), cf.sort_read = 1;
        if ((unsigned)cf.sort >= SORT_N) cf.sort = SORT_ALBUM;
        cf.albums = sorted();
    }
    if (n <= 0) {
        drop_albums(); /* only a real list is kept */
        cf.screen = COVERS; /* Return goes Home */
        list("Update Local Music first", 0);
        return;
    }
    unsigned count = deque_size(cf.albums);
    char path[512];
    if (!cf.saved_album) BLOB_IO(LAST_ALBUM, cf.saved_album, 0); /* outlives a reboot */
    cf.jobs = calloc(count, sizeof(job_t));
    for (unsigned i = 0; i < count; ++i) {
        void *r = deque_at(cf.albums, i);
        unsigned key = album_key(r);
        if (key == cf.saved_album && !on_sort) cf.album = (int)i;
        if (cf.jobs && P(r, REC_PATH) && access(art_path(path, key, ""), 0) &&
            (cf.jobs[cf.total].track = strdup(P(r, REC_PATH))))
            cf.jobs[cf.total++].key = key;
    }
    if (on_sort) cf.album = (int)count; /* the Sort card, pressed again and again */
    mkdir(ART_DIR, 0755);
    cf.done = cf.cancel = 0;
    if (cf.total && card_space(ART_DIR) && !pthread_create(&cf.thread, 0, worker, 0)) {
        cf.running = 1;
        cf.screen = PREPARING;
        page_row_detail(list("", 1), 0, "Cancel", 0, cancel_row);
        poll(0);
    } else
        to_covers(0);
}

/* Folder play (startPlayFolderSong): classType 1 over dq from track idx. playing_page's
 * mclLoadPlayList copies it synchronously, and memory-play later reloads the last track's folder.
 * Shared with navigation.c. */
void play_folder(void *dq, int idx) {
    struct {
        void *dq;
        int idx, cls, mode;
    } context = { dq, idx, 1, 2 };
    navigator_to_with_context("playing_page", &context);
}

static int play(void *ctx, void *event) {
    (void)event;
    int i = (int)(long)ctx;
    void *t = deque_at(cf.tracks, (unsigned)i);
    if (!t || access(P(t, REC_PATH), 0)) {
        widget_set_text_utf8(cf.title, "Storage unavailable");
        return 0;
    }
    play_folder(cf.tracks, i);
    return 0;
}

/* Album order: disc, then track, then path, so an untagged album keeps its file-name order and a
 * CUE image's tracks (one path) their start times. */
static int before(const void *pa, const void *pb) {
    void *a = *(void *const *)pa, *b = *(void *const *)pb;
    int d = I(a, REC_DISC) - I(b, REC_DISC);
    if (!d) d = I(a, REC_TRACK) - I(b, REC_TRACK);
    if (!d) d = strcmp(P(a, REC_PATH), P(b, REC_PATH));
    return d ? d : I(a, REC_CUE_START) - I(b, REC_CUE_START);
}

/* The tracks in album order, in a new deque; stock's name order when out of memory. */
static void *in_order(void *tracks) {
    unsigned n = deque_size(tracks);
    void **v = calloc(n + 1, sizeof *v);
    if (!v) return tracks;
    for (unsigned i = 0; i < n; ++i) v[i] = deque_at(tracks, i);
    qsort(v, n, sizeof *v, before);
    void *out = _create_deque("stSongInfo");
    deque_init(out);
    for (unsigned i = 0; i < n; ++i) _deque_push_back(out, v[i]);
    free(v);
    deque_destroy(tracks);
    return out;
}

/* A track's file name without its file's extension; a name not ending in it (CUE) stays whole.
 * Shared with navigation.c's scrobbler. */
const char *track_name(char *buf, unsigned size, void *t) {
    const char *name = P(t, REC_NAME), *path = P(t, REC_PATH), *ext = 0;
    if (!name || !path) return name;
    for (; *path; ++path)
        if (*path == '.')
            ext = path;
        else if (*path == '/')
            ext = 0;
    if (!ext) return name;
    unsigned n = strlen(name), e = strlen(ext);
    if (n <= e || strcmp(name + n - e, ext)) return name;
    snprintf(buf, size, "%.*s", (int)(n - e), name);
    return buf;
}

void *coverflow_album_tracks(void *r) {
    int n;
    return in_order(staged(albums, r, &n));
}

/* Album rows prefer the title tag. Untagged filenames lose a track-number prefix when it is
 * zero-padded, explicitly separated, or matches the track tag; numeric song titles stay whole. */
static const char *album_track_name(char *buf, unsigned size, void *t) {
    const char *title = P(t, REC_TITLE);
    if (title && *title) return title;
    const char *name = track_name(buf, size, t), *s = name;
    if (!s) return name;
    unsigned number = 0, digits = 0;
    while (*s >= '0' && *s <= '9' && digits < 3) {
        number = number * 10 + (unsigned)(*s++ - '0');
        ++digits;
    }
    if (!digits || !number || (*s >= '0' && *s <= '9')) return name;
    const char *end = s;
    while (*s == ' ' || *s == '\t') ++s;
    int separated = *s == '-' || *s == '.' || *s == '_';
    if (separated) ++s;
    while (*s == ' ' || *s == '\t') ++s;
    return *s && s != end && (separated || (digits > 1 && *name == '0') ||
                             number == (unsigned)I(t, REC_TRACK)) ? s : name;
}

static int to_tracks(const void *unused) {
    (void)unused;
    cf.timer = 0;
    int n;
    char name[512];
    void *r = deque_at(cf.albums, (unsigned)cf.album);
    cf.saved_album = album_key(r);
    BLOB_IO(LAST_ALBUM, cf.saved_album, 1); /* a power-off on the tracks keeps it too */
    if (cf.tracks) deque_destroy(cf.tracks);
    cf.tracks = coverflow_album_tracks(r);
    n = (int)deque_size(cf.tracks);
    cf.screen = TRACKS;
    widget_set_visible(cf.covers, 0, 0);
    void *view = list(P(r, REC_ALBUM), n);
    for (int i = 0; i < n; ++i)
        page_row_detail(view, i, album_track_name(name, sizeof name, deque_at(cf.tracks, (unsigned)i)), 0,
                        play);
    return 0;
}

static int refresh(const void *unused) {
    (void)unused;
    cf.timer = 0;
    void *dir = opendir(ART_DIR);
    for (struct dirent *e; dir && (e = readdir(dir));) {
        char path[600];
        snprintf(path, sizeof(path), ART_DIR "/%s", e->d_name);
        unlink(path); /* "." and ".." fail harmlessly */
    }
    if (dir) closedir(dir);
    drop_albums();
    load();
    return 0;
}

/* The Sort card: the next order, saved, and the covers again in it, still on the Sort card. The
 * stock list is kept, so nothing is queried but the order's own ranking. */
static int resort(const void *unused) {
    (void)unused;
    cf.timer = 0;
    cf.sort = (cf.sort + 1) % SORT_N;
    BLOB_IO(SORT_FILE, cf.sort, 1);
    if (cf.albums != cf.stock) deque_destroy(cf.albums);
    cf.albums = 0;
    cf.on_sort = 1;
    load();
    return 0;
}

/* Clicks only schedule: a screen change never destroys the widget whose click is running. */
static int pick(void *ctx, void *event) {
    (void)event;
    int i = (int)(long)ctx, n = (int)deque_size(cf.albums);
    if (i == n + 1)
        rearm(&cf.timer, refresh, 0);
    else if (i == n)
        rearm(&cf.timer, resort, 0);
    else {
        cf.album = i;
        rearm(&cf.timer, to_tracks, 0);
    }
    return 0;
}

/* Return: tracks -> covers (the slide_menu kept its album), preparing -> cancel and covers,
 * covers or the message -> Home. */
static int keyup(void *ctx, void *event) {
    (void)ctx;
    if (I(event, EVENT_KEY) != KEY_RETURN) return 0;
    if (cf.screen == TRACKS || cf.screen == PREPARING)
        rearm(&cf.timer, to_covers, 0);
    else
        navigator_back_to_home();
    return 11; /* RET_STOP */
}

static int closed(void *ctx, void *event) {
    (void)ctx;
    (void)event;
    if (cf.saved_album) BLOB_IO(LAST_ALBUM, cf.saved_album, 1);
    drop();
    cf.page = cf.body = cf.covers = cf.slide = 0;
    return 0;
}

static int coverflow_open(void *ctx, void *event) {
    (void)ctx;
    (void)event;
    if (cf.page || !(cf.page = page_open("coverflow_page", closed, keyup))) return 0;
    load();
    return 0;
}

/* The queue's playing record, or 0; *pos and *n get its index and the queue length. */
void *queue_now(unsigned *pos, unsigned *n) {
    void *queue = P(mcl_pdeqplaylist, 0);
    *pos = *(volatile unsigned *)MCL_POS;
    *n = queue ? deque_size(queue) : 0;
    return *pos < *n ? deque_at(queue, *pos) : (void *)0;
}

/* r's REC_ALBUM or REC_ARTIST as the player parsed it from the file, once it has parsed r's;
 * else the record's own. Folder-play records may carry no tags, so use the player's parsed
 * tags once they are available (issue #7). */
const char *now_tag(void *r, int field) {
    if (!r) return (void *)0;
    if (!tk_strcmp((const char *)g_play_id3_info, P(r, REC_PATH)))
        return (const char *)g_play_id3_info + (field == REC_ALBUM ? ID3_ALBUM : ID3_ARTIST);
    return P(r, field);
}

#if IPOD
/* iPod Home (docs/ipod.md): the playing track's art beside the list. */
static struct {
    void *win, *art, *list;
    unsigned key;
    int split_w;  /* the list's width in the asset */
    int panel[4]; /* the art's x, y, w, h in the asset: the right panel it fills */
    int clip[4];  /* the canvas clip while the art paints, restored after */
    int clipped;
} home __attribute__((section(".scratch")));
extern int ipod_home_full(void);

/* player_parsecover_thd writes the playing track's cover and then sets g_playcover_type, as Now
 * Playing reads it: 1 embedded, 2 folder image, 4 downloaded; 0 while parsing or stopped, 3 none.
 * Tidal's (5) is keyed by its online URL, never a queue path, so Home leaves it out. */
static const char *const player_covers[] = { 0, "file://" PEQ_ROOT "/tmp/coverpic.jpg",
                                             "file://" PEQ_ROOT "/tmp/externpic.jpg", 0,
                                             "file://" PEQ_ROOT "/tmp/externpic.jpg" };

/* Sizes the art to a w x h bitmap's proportions, just covering the panel and centred on it, so
 * the native fill draws it whole and the clip crops it evenly; unknown sizes fill the panel. */
static void home_fit(unsigned w, unsigned h) {
    int pw = home.panel[2], ph = home.panel[3], fw = pw, fh = ph;
    if (w && h && w <= 8192 && h <= 8192) {
        if ((unsigned)pw * h > (unsigned)ph * w)
            fh = (int)(((unsigned)pw * h + w - 1) / w);
        else
            fw = (int)(((unsigned)ph * w + h - 1) / h);
    }
    widget_move_resize(home.art, home.panel[0] + (pw - fw) / 2, home.panel[1] + (ph - fh) / 2, fw, fh);
}

/* The player's cover, else the Coverflow cache of the track's album (now_tag), else the
 * placeholder. The player's files belong to the track whose path it copies to g_lastcover_url
 * after writing them, so right after a track change they count only once that is this track. Runs
 * whenever Home or the status bar paints (at least once a second) and reloads only when the track,
 * the cover it can use or its parsed tags change. */
void coverflow_home_art(void *top) {
    if (!home.art || top != home.win || !widget_get_visible(home.art)) return;
    unsigned pos, n;
    void *r = queue_now(&pos, &n);
    const char *path = r ? P(r, REC_PATH) : (void *)0;
    unsigned char type = path && !tk_strcmp((const char *)g_lastcover_url, path) ? g_playcover_type : 0;
    unsigned album = tags_key(now_tag(r, REC_ARTIST), now_tag(r, REC_ALBUM));
    unsigned key = hash_bytes(hash_bytes(fnv(FNV_SEED, (const unsigned char *)path), &type, 1),
                              (const unsigned char *)&album, sizeof(album));
    if (key == home.key) return;
    home.key = key;
    const char *cover = type < sizeof(player_covers) / sizeof(*player_covers) ? player_covers[type] : 0;
    unsigned size[2] = { 0, 0 };
    int shown = cover && show(home.art, cover, size);
    if (!shown && r) {
        char url[600] = "file://";
        art_path(url + 7, album, "");
        shown = show(home.art, url, size);
    }
    if (!shown && !show(home.art, PLACEHOLDER, size)) image_base_set_image(home.art, PLACEHOLDER);
    home_fit(size[0], size[1]);
    widget_invalidate_force(home.art, 0);
}

/* The art paints only inside the panel: ringnav_paint_bg narrows the canvas clip (screen
 * coordinates; the canvas origin is the art's) before stock draws it, and ringnav_paint puts the
 * old clip back after. */
void coverflow_home_clip(void *w, void *canvas, int begin) {
    if (!w || w != home.art) return;
    if (!begin) {
        if (home.clipped) canvas_set_clip_rect(canvas, home.clip);
        home.clipped = 0;
        return;
    }
    int clip[4];
    clip_within(canvas, home.clip, clip, I(canvas, CANVAS_X) - I(w, W_X) + home.panel[0],
                I(canvas, CANVAS_Y) - I(w, W_Y) + home.panel[1], home.panel[2], home.panel[3]);
    canvas_set_clip_rect(canvas, clip);
    home.clipped = 1;
}

/* A widget and its descendants other than labels take the width, the list and its scroll view
 * `outer` and the rows and their tap images `inner`; labels keep theirs. */
static void home_width(void *w, int outer, int inner, int depth) {
    widget_move_resize(w, I(w, W_X), I(w, W_Y), depth < 2 ? outer : inner, I(w, W_H));
    for (unsigned i = 0, n = widget_count_children(w); i < n; ++i) {
        void *child = widget_get_child(w, i);
        if (tk_strcmp(widget_get_type(child), "hscroll_label")) home_width(child, outer, inner, depth + 1);
    }
}

/* The Home setting: Split keeps the asset's list and art; Full widens the list, so the selection
 * bar spans the window, and its rows and tap targets to HOME_FULL_ROW, so the chevrons mirror the
 * labels' margin clear of the corners, and hides the art. */
void coverflow_home_layout(void) {
    if (!home.list) return;
    int full = ipod_home_full();
    home_width(home.list, full ? 375 : home.split_w, full ? HOME_FULL_ROW : home.split_w, 0);
    widget_set_visible(home.art, !full, 0);
    home.key = ~0u; /* Split shows the current art again */
}
#endif

/* home_page_init: stock binds the name-matched img_* cards, then the Coverflow card binds here,
 * on its image as stock does, whatever stock returned. */
int coverflow_home(void *win, void *ctx) {
    int result = stock_home_trampoline(win, ctx);
    widget_on(widget_lookup(win, "img_coverflow", 1), EVT_CLICK, coverflow_open, 0);
#if IPOD
    home.win = win;
    void *art = widget_lookup(win, "img_homeart", 1);
    home.art = art;
    home.clipped = 0;
    for (int i = 0; art && i < 4; ++i) home.panel[i] = I(art, W_X + 4 * i); /* x, y, w, h */
    /* A fitted cover reaches under the list; taps there must still find the rows. */
    if (art) widget_set_sensitive(art, 0);
    void *list = widget_lookup(win, "list_view_home", 1);
    home.list = list; /* a new Home window's own, so still the asset's width */
    home.split_w = list ? I(list, W_W) : 0;
    coverflow_home_layout();
#endif
    return result;
}
