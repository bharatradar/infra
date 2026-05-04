FROM ghcr.io/bharatradar/mlat-server-sync-map:latest

RUN cat > /usr/share/nginx/html/index.html << 'HTMLEOF'
<!DOCTYPE html>
<html>
<head><meta http-equiv="refresh" content="0; url=/syncmap/"></head>
<body><a href="/syncmap/">MLAT Coverage Map</a></body>
</html>
HTMLEOF

RUN mkdir -p /usr/share/nginx/html/syncmap && \
cat > /usr/share/nginx/html/syncmap/index.html << 'HTMLEOF'
<html>

<head>
  <title>Bharat Radar MLAT Coverage</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.5.0/leaflet.css" />
  <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.5.0/leaflet-src.js"></script>
  <script src="https://code.jquery.com/jquery-3.5.0.min.js"></script>
  <script src="https://code.jquery.com/ui/1.12.1/jquery-ui.min.js"></script>
  <link rel="stylesheet" type="text/css" href="style.css" />
  <link rel="stylesheet" type="text/css" href="https://code.jquery.com/ui/1.12.1/themes/base/jquery-ui.css">
  <link rel="stylesheet" type="text/css"
    href="//fonts.googleapis.com/css?family=Source+Sans+Pro:200,400,200italic,300,300italic,400italic,600,600italic,700,700italic,900,900italic|Voltaire:400" />
  <script src="overlay.js"></script>
</head>

<body onload="initialize()">
  <div id="sidebar-container">
    <h1>Bharat Radar MLAT feeder map</h1>
    <p class="note"><b>For privacy: locations are approximate</b></p>
    <p class="note"><b>For privacy: The Bharat Radar feed client sends a privacy flag by default.
        To disable it, <a href="https://github.com/bharatradar/infra#feeder">see the infra
          repo.</a>
      </b>
    </p>
    <div id="progressbar">
      <p id="progresslabel" class="progresslabel">Loading...</p>
    </div>
    <div id="sidebar-content">
      <p><b>Feeders are operated by people just like you.</b> For
        information on how you can become a feeder, contribute data to
        the this network,
        <a href="https://bharat-radar.vellur.in/feed/" target="_blank">click here</a>.
      </p>
      <p>
        Click or hover over a feeder on the map to see the peers it's
        synchronized with for multilateration. Line color indicates
        quality of the peer synchronization.
      </p>
      <input type="text" id="search-input" onkeyup="searchHandler()" placeholder="Search for a feeder"
        style="width: 100%">
      <div id="stations-container">
        <ul id="stations">
        </ul>
      </div>
      <div id="station-info" class="hidden">
        <h2> <span id="si-name">Feeder name</span></h2>
        <div><span class="si-label" id="si-num-label"># synced peers:</span> <span id="si-num-peers">1000</span>
          <span><a id="si-sync-stats-link">Sync stats</a></span>
        </div>
        <div><span class="si-label">Region:</span> <span id="si-region">region</span></div>
        <div><span class="si-label">Closest peer:</span> <span id="si-closest-peer-dist">0</span></div>
        <div><span class="si-label">Farthest peer:</span> <span id="si-farthest-peer-dist">0</span></div>
        <div><span class="si-label">Approximate location:</span> <span id="si-peer-loc">0</span></div>
      </div>
    </div>
  </div>
  <div id="map-container">
    <div id="map-canvas"></div>
  </div>
</body>

</html>
HTMLEOF

RUN cat > /etc/nginx/conf.d/default.conf << 'NGINXEOF'
server {
    listen 80;
    server_name localhost;

    location /api/0/mlat-server/ {
        rewrite ^/api/0/mlat-server/[^/]+/(.*)$ /$1 break;
        proxy_pass http://mlat-mlat-server.bharatradar.svc.cluster.local:150;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location / {
        root /usr/share/nginx/html;
        index index.html index.htm;
    }
}
NGINXEOF
