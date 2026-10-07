/* Shared by the firmware and the native pixel comparison; no toolkit or pointer-layout ABI. */
#define ART_SIZE 160

/* Depth (docs/internals.md#coverflow-depth), after PictureFlow's renderer: every cover within reach
 * of the visual position is projected column by column into one frame bitmap, nearest first, with
 * its reflection, then the frame is drawn over the slide_menu, whose own children stay empty. All
 * integer: 16.16 turns and rotations, geometry in 1/16 px, row spans in 1/256 px. */
#define CF_ONE 65536
#define CF_REACH 3 /* ring slots either side of the centre that can show (fading in) or preload */
#define CF_RING (2 * CF_REACH + 1)
#define CF_TEXELS (ART_SIZE * ART_SIZE)
#define CF_EYE16 (16 * CF_EYE)
#define CF_HALF16 (16 * ART_SIZE / 2)
#define CF_MID256 (256 * (CF_TOP + ART_SIZE / 2)) /* the horizon every cover centres on */

/* cos and sin of 0 to CF_ANGLE (60) degrees in 16 steps, 16.16 */
static const int cf_cos[17] = { 65536, 65396, 64975, 64277, 63303, 62058, 60547, 58777, 56756,
                                54491, 51993, 49273, 46341, 43211, 39896, 36410, 32768 };
static const int cf_sin[17] = { 0,     4286,  8554,  12785, 16962, 21066, 25080, 28986, 32768,
                                36410, 39896, 43211, 46341, 49273, 51993, 54491, 56756 };

/* A right-hand cover t (16.16, >= 0) from the visual position: centre xc and depth zc (1/16 px),
 * rotation c, s (16.16) and brightness (of 256). Left covers mirror it. */
typedef struct {
    int xc, zc, c, s, bright;
} pose_t;

static void pose(int t, pose_t *p) {
    int a = t < CF_ONE ? t : CF_ONE, i = a >> 12, f = a & 4095;
    p->c = cf_cos[i] + (i < 16 ? (cf_cos[i + 1] - cf_cos[i]) * f >> 12 : 0);
    p->s = cf_sin[i] + (i < 16 ? (cf_sin[i + 1] - cf_sin[i]) * f >> 12 : 0);
    if (t < CF_ONE) {
        /* xc = A t + B t^2: CF_X1 at t = 1, arriving at the stack's CF_XS slope, and quick at
         * first, so the covers turning in and out mid-step keep clear of each other. */
        int sq = (int)((unsigned)t * (unsigned)t >> 16);
        p->xc = (16 * (2 * CF_X1 - CF_XS) * t + 16 * (CF_XS - CF_X1) * sq) >> 16;
        p->zc = 16 * CF_Z1 * t >> 16;
        p->bright = 256 - ((256 - CF_BRIGHT1) * t >> 16);
    } else {
        int e = t - CF_ONE;
        p->xc = 16 * CF_X1 + (16 * CF_XS * e >> 16);
        p->zc = 16 * CF_Z1 + (16 * CF_ZS * e >> 16);
        p->bright = e < CF_ONE ? CF_BRIGHT1 - ((CF_BRIGHT1 - CF_BRIGHT2) * e >> 16)
                    : e < 2 * CF_ONE ? CF_BRIGHT2 * (2 * CF_ONE - e) >> 16 : 0;
    }
}

/* Screen x (1/16 px right of the centre line) of the cover point u (1/16 px from its middle). */
static int project(const pose_t *p, int u) {
    return (p->xc + (u * p->c >> 16)) * CF_EYE16 / (CF_EYE16 + p->zc - (u * p->s >> 16));
}

/* The cover point under screen x dx (1/16 px right of the centre line), the inverse of project;
 * 0 when the column misses the cover. *h gets the cover's projected height there (1/256 px). */
static int unproject(const pose_t *p, int dx, int *u, int *h) {
    int den = (CF_EYE16 * p->c + dx * p->s) >> 16;
    if (den <= 0) return 0;
    *u = (dx * (CF_EYE16 + p->zc) - CF_EYE16 * p->xc) / den;
    if (*u < -CF_HALF16 || *u >= CF_HALF16) return 0;
    *h = ART_SIZE * 256 * CF_EYE16 / (CF_EYE16 + p->zc - (*u * p->s >> 16));
    return 1;
}

#define HOT static inline __attribute__((always_inline)) /* -Oz would call these per pixel */

/* p scaled by b (of 256), per 8-bit channel; alpha is left 0. */
HOT unsigned shade(unsigned p, unsigned b) {
    return ((p & 0xff00ffu) * b >> 8 & 0xff00ffu) | ((p & 0xff00u) * b >> 8 & 0xff00u);
}

/* The frame being drawn, nearest cover first: each pixel's alpha byte holds the coverage so far
 * (255 opaque), and a layer only fills what is left, which composites as drawing back to front
 * would. Per column, rows solid_top to solid_bottom are known opaque and skipped outright, so the
 * hidden parts of side covers cost nothing. */
typedef struct {
    unsigned *d;
    int pitch;
    short solid_top[CF_VIEW_W], solid_bottom[CF_VIEW_W];
} shelf_t;

/* colour (alpha ignored) under what is already at px, covering cov (of 256) of the pixel */
HOT void put(unsigned *px, unsigned colour, unsigned cov) {
    unsigned a = *px >> 24, have = a + (a >> 7), w = cov * (256 - have) >> 8;
    if (!a && cov == 256) {
        *px = colour | 0xff000000u;
        return;
    }
    if (!w) return;
    have += w;
    *px = ((*px & 0xffffffu) + shade(colour, w)) | (have > 255 ? 255u : have) << 24;
}

/* One texture column (texels ART_SIZE apart) h high (1/256 px) on the horizon at frame column col,
 * its partial end rows by coverage, then its reflection fading out below. */
static void column(shelf_t *f, int col, const unsigned *tex, int h, int bright) {
    if (h < 16) return;
    unsigned *d = f->d + col;
    int top = CF_MID256 - h / 2, bottom = top + h, step = (ART_SIZE << 20) / (h >> 4);
    int solid_top = f->solid_top[col], solid_bottom = f->solid_bottom[col];
    int y = top >> 8, end = (bottom + 255) >> 8, last = end < CF_VIEW_H ? end : CF_VIEW_H;
    int first = y, final = end - 1; /* the rows the edges cross */
    int v = (((y << 8) + 128 - top) >> 4) * step >> 4, pitch = f->pitch; /* v: 16.16 texel row */
    if (y < 0) v -= y * step, y = 0;
    unsigned *px = d + y * pitch;
    while (y < last) {
        if (y >= solid_top && y < solid_bottom) {
            int skip = solid_bottom - y;
            v += skip * step, px += skip * pitch, y = solid_bottom;
            continue;
        }
        int run = y < solid_top && solid_top < last ? solid_top : last; /* up to the opaque span */
        for (; y < run; ++y, v += step, px += pitch) {
            int row = v < 0 ? 0 : v >> 16 >= ART_SIZE ? ART_SIZE - 1 : v >> 16;
            unsigned cov = 256;
            if (y == first || y == final) {
                int from = y << 8 > top ? y << 8 : top, to = (y + 1) << 8 < bottom ? (y + 1) << 8 : bottom;
                cov = (unsigned)(to - from);
            }
            put(px, shade(tex[row * ART_SIZE], (unsigned)bright), cov);
        }
    }
    /* The reflection starts where the body ends, sharing the row the body only partly covers. */
    int reflect = h * CF_REFLECT / ART_SIZE, stop = (bottom + reflect + 255) >> 8;
    int fade = (256 << 16) / reflect; /* fading per 1/256 px, 16.16 */
    unsigned dim = (unsigned)bright * CF_REFLECT_TOP >> 8;
    if (stop > CF_VIEW_H) stop = CF_VIEW_H;
    for (y = bottom >> 8, px = d + y * pitch; y < stop; ++y, px += pitch) {
        int from = y << 8 > bottom ? y << 8 : bottom, dist = ((from + ((y + 1) << 8)) >> 1) - bottom;
        if (dist >= reflect) break;
        if (y < 0 || (y >= solid_top && y < solid_bottom)) continue;
        int mirrored = dist * step >> 8 >> 16; /* texel rows up from the bottom edge */
        int row = ART_SIZE - 1 - (mirrored >= ART_SIZE ? ART_SIZE - 1 : mirrored);
        unsigned cov = (unsigned)(((y + 1) << 8) - from) * (256u - (unsigned)(dist * fade >> 16)) >> 8;
        put(px, shade(tex[row * ART_SIZE], dim), cov);
    }
    /* The rows this body covers whole are opaque now: join them to the known span, or keep the
     * longer of the two. */
    int s0 = (top + 255) >> 8, s1 = bottom >> 8;
    s0 = s0 < 0 ? 0 : s0, s1 = s1 > CF_VIEW_H ? CF_VIEW_H : s1;
    if (s1 <= s0) return;
    if (solid_bottom > solid_top && s0 <= solid_bottom && s1 >= solid_top) {
        s0 = s0 < solid_top ? s0 : solid_top;
        s1 = s1 > solid_bottom ? s1 : solid_bottom;
    } else if (s1 - s0 <= solid_bottom - solid_top)
        return;
    f->solid_top[col] = (short)s0, f->solid_bottom[col] = (short)s1;
}

/* The cover t (16.16, signed) turns from the visual position, under what is drawn. */
static void draw_cover(shelf_t *f, int t, const unsigned *tex) {
    pose_t p;
    int side = t < 0 ? -1 : 1;
    pose(t * side, &p);
    if (p.bright <= 0 || !tex) return;
    int a = project(&p, -CF_HALF16), b = project(&p, CF_HALF16);
    int lo = (16 * CF_CX + (a < b ? a : b)) >> 4, hi = (16 * CF_CX + (a < b ? b : a)) >> 4;
    for (int v = lo; v <= hi; ++v) { /* v: the column as if the cover were on the right */
        int col = side > 0 ? v : 2 * CF_CX - 1 - v, u, h;
        if (col < 0 || col >= CF_VIEW_W || !unproject(&p, (2 * v + 1 - 2 * CF_CX) * 8, &u, &h)) continue;
        int k = (u + CF_HALF16) >> 4;
        column(f, col, tex + (side > 0 ? k : ART_SIZE - 1 - k), h, p.bright);
    }
}

/* The ring slot of the k-th nearest pair's nearer (i 0) or farther (i 1) cover at frac: the side
 * the position is moving away from is farther. */
static int slot(int frac, int k, int i) {
    return (i == 0) == (frac < 0) ? CF_REACH - k : CF_REACH + k;
}

/* The shelf at frac (16.16, -CF_ONE/2 to under CF_ONE/2) past its centre cover: ring[j] is the
 * texture of the cover j - CF_REACH from the centre, or 0 to leave it out. Nearest first; nearer
 * covers and their reflections overlap farther ones. */
void coverflow_render(unsigned *d, int pitch, int frac, const unsigned *const ring[CF_RING]) {
    shelf_t f;
    f.d = d, f.pitch = pitch;
    for (int x = 0; x < CF_VIEW_W; ++x) f.solid_top[x] = f.solid_bottom[x] = 0;
    for (int y = 0; y < CF_VIEW_H; ++y) memset(d + y * pitch, 0, CF_VIEW_W * 4);
    for (int k = 0; k <= CF_REACH; ++k)
        for (int i = 0; i < (k ? 2 : 1); ++i) {
            int j = slot(frac, k, i);
            draw_cover(&f, (j - CF_REACH) * CF_ONE - frac, ring[j]);
        }
    for (int y = 0; y < CF_VIEW_H; ++y) { /* over black; a count-down loop, which -Oz keeps tight */
        unsigned *p = d + y * pitch;
        int x = CF_VIEW_W;
        do *p++ |= 0xff000000u;
        while (--x);
    }
}

