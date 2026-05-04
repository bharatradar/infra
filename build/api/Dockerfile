FROM ghcr.io/bharatradar/api:latest

ENV MY_DOMAIN=bharat-radar.vellur.in

COPY patch.py /tmp/patch.py
RUN python3 /tmp/patch.py && rm /tmp/patch.py
