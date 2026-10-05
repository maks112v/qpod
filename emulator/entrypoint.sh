#!/bin/sh
set -eu

root=/q2
shim=/work/q2emu.so

mips64el-linux-gnuabi64-gcc -mabi=n32 -shared -fPIC -O2 -Wall -Wextra \
    -o "$shim" /src/q2emu.c -ldl

if [ ! -x "$root/qemu-mipsel-static" ]; then
    cp /usr/bin/qemu-mipsel-static "$root/qemu-mipsel-static"
fi
cp "$shim" "$root/q2emu.so"
mkdir -p "$root/run/dbus" "$root/dev/input" "$root/tmp"
touch "$root/dev/null"

# D-Bus is part of the normal firmware boot and several background services
# assume its system socket exists even when their hardware is absent.
chroot "$root" /qemu-mipsel-static /usr/bin/dbus-daemon \
    --config-file=/etc/dbus-1/system.conf || true

exec chroot "$root" /qemu-mipsel-static -E LD_PRELOAD=/q2emu.so /release/bin/demo
