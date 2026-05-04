#!/bin/bash
# Update SSL certificates for bharatradar.com and proxy all subdomains to FRP
# Run with: sudo bash update-ssl-bharatradar.sh

set -euo pipefail

DOMAIN="bharatradar.com"
SUBDOMAINS="map mlat history api my ws feed"
ALL_DOMAINS="$DOMAIN"
for sub in $SUBDOMAINS; do
    ALL_DOMAINS="$ALL_DOMAINS $sub.$DOMAIN"
done

# Step 1: Issue certificates (stop nginx to free port 80)
echo ">>> Issuing Let's Encrypt certificates for $ALL_DOMAINS"
CERTBOT_DOMAINS=""
for d in $ALL_DOMAINS; do
    CERTBOT_DOMAINS="$CERTBOT_DOMAINS -d $d"
done

sudo systemctl stop nginx

sudo certbot certonly --standalone \
    --non-interactive \
    --agree-tos \
    --register-unsafely-without-email \
    $CERTBOT_DOMAINS \
    --force-renewal || {
    echo "Certbot failed. Check errors above."
    sudo systemctl start nginx
    exit 1
}

sudo systemctl start nginx

echo ">>> Certificates issued successfully"

# Step 2: Write ACME challenge HTTP server
sudo tee /etc/nginx/sites-enabled/bharatradar-http > /dev/null <<EOF
# HTTP - ACME challenge for all bharatradar.com domains
server {
    listen 80;
    server_name ${ALL_DOMAINS// / };

    location ^~ /.well-known/ {
        root /var/www/html;
        allow all;
    }

    # Redirect all other traffic to HTTPS
    location / {
        return 301 https://\$host\$request_uri;
    }
}
EOF

# Step 3: Write SSL vhost for main domain (with WebSocket support)
sudo tee /etc/nginx/sites-enabled/bharatradar-ssl > /dev/null <<EOF
# HTTPS - Main domain with WebSocket support via /ws/ path
server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    # WebSocket path
    location /ws/ {
        proxy_pass http://127.0.0.1:8080/ws/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host ws.${DOMAIN};
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400;
    }

    # Main app - proxy to FRP
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

# Step 4: Write SSL vhosts for each subdomain
sudo tee /etc/nginx/sites-enabled/bharatradar-subdomains > /dev/null <<EOF
# HTTPS - Map subdomain
server {
    listen 443 ssl http2;
    server_name map.${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

# HTTPS - MLAT subdomain
server {
    listen 443 ssl http2;
    server_name mlat.${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

# HTTPS - History subdomain
server {
    listen 443 ssl http2;
    server_name history.${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

# HTTPS - API subdomain
server {
    listen 443 ssl http2;
    server_name api.${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

# HTTPS - My subdomain
server {
    listen 443 ssl http2;
    server_name my.${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

# HTTPS - WS subdomain
server {
    listen 443 ssl http2;
    server_name ws.${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400;
    }
}

# HTTPS - Feed subdomain
server {
    listen 443 ssl http2;
    server_name feed.${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

# Step 5: Remove old vhosts
sudo rm -f /etc/nginx/sites-enabled/bharat-radar-ssl \
           /etc/nginx/sites-enabled/bharat-radar-subdomains \
           /etc/nginx/sites-enabled/acme-challenge

# Step 6: Test and reload
echo ">>> Testing nginx configuration..."
sudo nginx -t
echo ">>> Reloading nginx..."
sudo systemctl reload nginx

echo ""
echo ">>> Done! Certificates and nginx configured for ${DOMAIN}"
echo ">>> Certificate expires: $(sudo openssl x509 -enddate -noout -in /etc/letsencrypt/live/${DOMAIN}/fullchain.pem)"
echo ">>> Run this script again to renew (or set up cron for certbot renew)"
