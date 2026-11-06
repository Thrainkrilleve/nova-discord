# Speech to text: whisper.cpp's server, on CPU (plenty for voice-message clips; the GPU stays with
# Nova's model). The model downloads on first start into the models volume.
# GGML_NATIVE=OFF builds for any x86-64 with AVX2, not just the CPU that built it: an image built on the
# desktop crashed with "illegal instruction" (exit 132) on the server.
FROM debian:bookworm-slim AS build
ARG WHISPER_REF=v1.9.5
RUN apt-get update && apt-get install -y --no-install-recommends git cmake build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${WHISPER_REF}" https://github.com/ggml-org/whisper.cpp /src \
    && cmake -S /src -B /src/build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF \
    && cmake --build /src/build -j --target whisper-server

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/bin/whisper-server /usr/local/bin/whisper-server
COPY deploy/whisper-entrypoint.sh /usr/local/bin/whisper-entrypoint.sh
RUN chmod +x /usr/local/bin/whisper-entrypoint.sh && useradd --create-home --uid 1000 whisper \
    && mkdir -p /models && chown whisper /models
USER whisper
ENV WHISPER_MODEL=ggml-small-q5_1.bin WHISPER_THREADS=8
EXPOSE 8003
ENTRYPOINT ["/usr/local/bin/whisper-entrypoint.sh"]
