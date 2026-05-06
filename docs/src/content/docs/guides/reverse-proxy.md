---
title: Reverse proxy setup
description: Put the search API behind Apache or Nginx with HTTPS and CORS.
---

If you want to expose the server over HTTPS with a clean hostname (e.g. for VPN access from off-LAN clients), put it behind a reverse proxy. The two things that need attention are **CORS preflight** and **header duplication**. Get those wrong and the addon will fail with `NetworkError` even though the server itself is healthy.

## Why CORS matters here

The Thunderbird addon makes requests from a `moz-extension://…` origin to your server. Browsers treat that as cross-origin, so the addon's `fetch()` always:

1. Sends an `OPTIONS` preflight first.
2. Only sends the real request if the preflight response has the right `Access-Control-Allow-*` headers.

The FastAPI server already sets these headers via its `CORSMiddleware`. Your proxy must either pass them through cleanly, or strip them and set its own, but **not both** at the same time, or the browser sees duplicates and rejects the response.

## Apache 2.4

### Required modules

```bash
sudo a2enmod headers rewrite proxy proxy_http ssl
sudo systemctl restart apache2
```

### Vhost (anonymized)

Replace `mail-search.example.com` with your hostname and `192.168.x.x` with your server's IP (or `localhost` if Apache and the FastAPI container run on the same host).

```apache
<VirtualHost *:80>
    ServerName mail-search.example.com

    # Redirect all HTTP to HTTPS, except ACME challenges
    RewriteEngine On
    RewriteCond %{REQUEST_URI} !^/\.well-known/acme-challenge/ [NC]
    RewriteCond %{HTTPS} !=on
    RewriteRule ^ https://%{SERVER_NAME}%{REQUEST_URI} [END,NE,R=permanent]

    DocumentRoot /var/www/html/
</VirtualHost>

<VirtualHost *:443>
    ServerName mail-search.example.com
    DocumentRoot /var/www/html/

    SSLEngine on
    SSLCertificateFile    /etc/apache2/ssl/your-cert.crt
    SSLCertificateKeyFile /etc/apache2/ssl/your-cert.key
    # Only needed if your cert is signed by a private CA:
    # SSLCACertificateFile /etc/apache2/ssl/your-ca.crt

    CustomLog ${APACHE_LOG_DIR}/mail-search_access.log combined
    ErrorLog  ${APACHE_LOG_DIR}/mail-search_error.log

    # --- CORS ---
    # Strip upstream CORS headers from the main response table so they don't
    # duplicate with the ones we set below. NOTE: 'Header unset' without
    # 'always' is required. 'Header always unset' touches a different table
    # and won't remove headers added by the proxied backend.
    Header unset Access-Control-Allow-Origin
    Header unset Access-Control-Allow-Credentials
    Header unset Access-Control-Allow-Methods
    Header unset Access-Control-Allow-Headers
    Header unset Access-Control-Expose-Headers

    # Set our own. 'always' so they're attached to error responses too.
    Header always set Access-Control-Allow-Origin  "*"
    Header always set Access-Control-Allow-Methods "GET, POST, OPTIONS"
    Header always set Access-Control-Allow-Headers "Content-Type, X-API-Key"
    Header always set Access-Control-Max-Age       "600"

    # Short-circuit OPTIONS preflight with 204. Never proxied to FastAPI.
    RewriteEngine On
    RewriteCond %{REQUEST_METHOD} =OPTIONS
    RewriteRule ^ - [R=204,L]

    # Proxy everything else to the FastAPI server.
    ProxyRequests Off
    ProxyPreserveHost On
    ProxyTimeout 60
    ProxyPass        / http://192.168.x.x:8342/
    ProxyPassReverse / http://192.168.x.x:8342/
</VirtualHost>
```

### Why each piece is there

| Directive | Why |
|-----------|-----|
| `Header unset …` (no `always`) | Removes the CORS headers FastAPI sets on real responses, so the proxy doesn't end up sending each header twice. `Header always unset` operates on a different internal header table and silently does nothing here. |
| `Header always set …` | Apache becomes the authoritative source for CORS. `always` ensures the headers attach to error responses too, so the addon gets actionable 4xx/5xx error bodies. |
| `RewriteRule ^ - [R=204,L]` for OPTIONS | Returns 204 with the CORS headers above directly from Apache. Avoids any chance of the backend interfering with preflight. |
| `ProxyPreserveHost On` | Keeps the original `Host` header so FastAPI sees the real hostname (useful for logging and some auth flows). |
| `ProxyTimeout 60` | Search is fast (sub-second on small indices) but indexing endpoints can take longer. Adjust if you trigger long operations through the proxy. |

### Verification

After `sudo apachectl configtest && sudo systemctl reload apache2`, run from a machine *other* than the Apache server (so you cross the network):

```bash
# Preflight: must be 204 with Access-Control-Allow-* headers.
curl -i -X OPTIONS https://mail-search.example.com/health \
  -H 'Origin: moz-extension://test' \
  -H 'Access-Control-Request-Method: GET' \
  -H 'Access-Control-Request-Headers: content-type, x-api-key'

# Real call: must be 200, with exactly ONE Access-Control-Allow-Origin.
curl -i https://mail-search.example.com/health \
  -H 'Origin: moz-extension://test' \
  -H 'X-API-Key: <your-api-key>' \
  | grep -ic '^access-control-allow-origin'
# Expect: 1
```

If the count is `2`, your `Header unset` lines aren't taking effect. Check for stray `always` keywords on them, or that the directives are inside the `<VirtualHost *:443>` block.

If you're using a private/self-signed certificate, see the [custom certificate guide](/thunderbird-ai-search/guides/custom-certificate/) before pointing the addon at this URL.

## Nginx

The same shape, simpler syntax:

```nginx
server {
    listen 443 ssl http2;
    server_name mail-search.example.com;

    ssl_certificate     /etc/nginx/ssl/your-cert.crt;
    ssl_certificate_key /etc/nginx/ssl/your-cert.key;

    # Authoritative CORS headers (use 'always' so 4xx/5xx get them too)
    add_header Access-Control-Allow-Origin  "*"                       always;
    add_header Access-Control-Allow-Methods "GET, POST, OPTIONS"      always;
    add_header Access-Control-Allow-Headers "Content-Type, X-API-Key" always;
    add_header Access-Control-Max-Age       "600"                     always;

    location / {
        # Short-circuit OPTIONS preflight with 204
        if ($request_method = OPTIONS) {
            return 204;
        }

        # Hide upstream CORS headers so they don't duplicate with ours
        proxy_hide_header Access-Control-Allow-Origin;
        proxy_hide_header Access-Control-Allow-Credentials;
        proxy_hide_header Access-Control-Allow-Methods;
        proxy_hide_header Access-Control-Allow-Headers;
        proxy_hide_header Access-Control-Expose-Headers;

        proxy_pass         http://192.168.x.x:8342;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

`proxy_hide_header` is Nginx's equivalent of Apache's `Header unset`. Same idea: drop what FastAPI added so `add_header` doesn't compound it.

## Pointing the addon at the proxy

In Thunderbird → **Add-ons Manager → AI Email Search → Options**, set the **Server URL** to your proxy URL (e.g. `https://mail-search.example.com`). Save and reload the addon.

## Common pitfalls

- **`NetworkError` only on `POST /search` (not `GET /health`)**: usually means duplicate `Access-Control-Allow-Origin` headers. Check with the curl `grep -ic` shown above.
- **`Status code: (null)` in the browser console**: the preflight didn't get an HTTP response at all. Either TLS handshake failed (wrong or untrusted certificate, see the [custom certificate guide](/thunderbird-ai-search/guides/custom-certificate/)), or the rewrite for OPTIONS isn't matching.
- **Empty Apache error log**: TLS rejection by the *client* doesn't write to the vhost error log. Verify the cert chain with `curl --cacert <ca.crt> https://your-host/health` from the client machine.
