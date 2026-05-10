# This image is built by infra CI from the bharatradar/mlat-server-sync-map fork (master branch).
# For local builds, use the fork source directly.
FROM nginx:alpine
COPY mlat-server-sync-map/. /usr/share/nginx/html
COPY infra/build/mlat-server-sync-map/syncmap/index.html /usr/share/nginx/html/syncmap/index.html
COPY infra/build/mlat-server-sync-map/syncmap/overlay.js /usr/share/nginx/html/syncmap/overlay.js
