FROM ghcr.io/bharatradar/sdr-enthusiasts-docker-tar1090:latest AS base
FROM ghcr.io/bharatradar/wiedehopf-readsb:latest AS uuid

FROM base
COPY --from=uuid /usr/local/bin/readsb-with-uuids /usr/local/bin/readsb
COPY --from=uuid /usr/local/bin/viewadsb-uuid /usr/local/bin/viewadsb
COPY --from=uuid /usr/local/lib/libjemalloc.so.2 /usr/local/lib/libjemalloc.so.2
COPY --from=uuid /usr/local/share/tar1090/aircraft.csv.gz /usr/local/share/tar1090/aircraft.csv.gz
ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so.2
