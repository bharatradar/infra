# This image is built by infra CI from the bharatradar/api fork (main branch).
# This Dockerfile is a placeholder for local builds.
# For CI, the fork source is checked out directly.
FROM ghcr.io/bharatradar/api:latest

ENV MY_DOMAIN=bharatradar.com

COPY patch.py /tmp/patch.py
RUN python3 /tmp/patch.py && rm /tmp/patch.py
