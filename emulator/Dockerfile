FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc-multilib-mips64el-linux-gnuabi64 \
        libc6-dev-mipsn32-mips64el-cross \
        qemu-user-static \
    && rm -rf /var/lib/apt/lists/*

COPY emulator/entrypoint.sh /usr/local/bin/q2-emulator
ENTRYPOINT ["/usr/local/bin/q2-emulator"]
