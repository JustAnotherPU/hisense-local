#!/bin/sh
# Start the gateway. The certificate is made once and kept in /data.
set -eu
umask 077
TLS=/data/tls
mkdir -p "$TLS"
chmod 700 /data "$TLS"
if [ ! -s "$TLS/cert.pem" ] || [ ! -s "$TLS/key.pem" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=hisense-local" \
        -keyout "$TLS/key.pem" -out "$TLS/cert.pem" 2>/dev/null
    chmod 600 "$TLS/key.pem" "$TLS/cert.pem"
fi
cd /app
exec python3 -m hisense_local run
