#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

enum device_kind { DEVICE_NONE, DEVICE_FB, DEVICE_INPUT, DEVICE_DAC };

static enum device_kind devices[1024];

static enum device_kind path_kind(const char *path) {
    if (path == NULL) return DEVICE_NONE;
    if (strcmp(path, "/dev/fb0") == 0) return DEVICE_FB;
    if (strncmp(path, "/dev/input/event", 16) == 0) return DEVICE_INPUT;
    if (strcmp(path, "/dev/shanling_dac") == 0) return DEVICE_DAC;
    return DEVICE_NONE;
}

static void remember(int fd, const char *path) {
    if (fd >= 0 && fd < (int)(sizeof(devices) / sizeof(devices[0]))) {
        devices[fd] = path_kind(path);
    }
}

int open(const char *path, int flags, ...) {
    static int (*real_open)(const char *, int, ...) = NULL;
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    if (real_open == NULL) real_open = dlsym(RTLD_NEXT, "open");
    int fd = real_open(path, flags, mode);
    remember(fd, path);
    return fd;
}

int open64(const char *path, int flags, ...) {
    static int (*real_open64)(const char *, int, ...) = NULL;
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    if (real_open64 == NULL) real_open64 = dlsym(RTLD_NEXT, "open64");
    int fd = real_open64(path, flags, mode);
    remember(fd, path);
    return fd;
}

int close(int fd) {
    static int (*real_close)(int) = NULL;
    if (real_close == NULL) real_close = dlsym(RTLD_NEXT, "close");
    if (fd >= 0 && fd < (int)(sizeof(devices) / sizeof(devices[0]))) {
        devices[fd] = DEVICE_NONE;
    }
    return real_close(fd);
}

static int framebuffer_ioctl(unsigned long request, void *arg) {
    if (request == FBIOGET_FSCREENINFO) {
        struct fb_fix_screeninfo *fix = arg;
        memset(fix, 0, sizeof(*fix));
        memcpy(fix->id, "q2-emulator", 11);
        fix->smem_len = 320 * 375 * 4 * 2;
        fix->type = FB_TYPE_PACKED_PIXELS;
        fix->visual = FB_VISUAL_TRUECOLOR;
        fix->line_length = 320 * 4;
        return 0;
    }
    if (request == FBIOGET_VSCREENINFO) {
        struct fb_var_screeninfo *var = arg;
        memset(var, 0, sizeof(*var));
        var->xres = var->xres_virtual = 320;
        var->yres = 375;
        var->yres_virtual = 750;
        var->bits_per_pixel = 32;
        var->red.offset = 16; var->red.length = 8;
        var->green.offset = 8; var->green.length = 8;
        var->blue.offset = 0; var->blue.length = 8;
        var->transp.offset = 24; var->transp.length = 8;
        var->activate = FB_ACTIVATE_NOW;
        return 0;
    }
    if (request == FBIOPAN_DISPLAY) {
        const struct fb_var_screeninfo *var = arg;
        int page = var != NULL && var->yoffset >= 375;
        int fd = open("/tmp/q2-emulator-page.tmp", O_WRONLY | O_CREAT | O_TRUNC, 0666);
        if (fd >= 0) {
            char value = (char)('0' + page);
            (void)write(fd, &value, 1);
            close(fd);
            (void)rename("/tmp/q2-emulator-page.tmp", "/tmp/q2-emulator-page");
        }
        return 0;
    }
    if (request == FBIO_WAITFORVSYNC || request == FBIOPUT_VSCREENINFO) return 0;
    errno = ENOTTY;
    return -1;
}

int ioctl(int fd, unsigned long request, ...) {
    static int (*real_ioctl)(int, unsigned long, ...) = NULL;
    void *arg;
    va_list ap;
    va_start(ap, request);
    arg = va_arg(ap, void *);
    va_end(ap);
    if (real_ioctl == NULL) real_ioctl = dlsym(RTLD_NEXT, "ioctl");

    enum device_kind kind = DEVICE_NONE;
    if (fd >= 0 && fd < (int)(sizeof(devices) / sizeof(devices[0]))) kind = devices[fd];
    if (kind == DEVICE_FB) return framebuffer_ioctl(request, arg);
    if (kind == DEVICE_DAC) return 0;
    if (kind == DEVICE_INPUT) {
        if (request == EVIOCGVERSION && arg != NULL) *(int *)arg = 0x010001;
        return 0;
    }
    return real_ioctl(fd, request, arg);
}

int system(const char *command) {
    (void)command;
    return 0;
}
