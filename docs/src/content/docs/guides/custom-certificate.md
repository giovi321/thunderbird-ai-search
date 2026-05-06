---
title: Custom certificate / private CA
description: Make Thunderbird trust a self-signed cert or private CA so the addon can talk to your server over HTTPS.
---

If your server isn't reachable from the public internet (typical: VPN-only, internal LAN), you can't get a Let's Encrypt certificate via the standard HTTP-01 challenge. The two practical options are:

1. **DNS-01 challenge with Let's Encrypt** — works for non-public servers if your DNS provider has an API. Best long-term solution; out of scope here.
2. **Use a private CA / self-signed certificate and tell Thunderbird to trust it.** This guide covers that.

## Symptom this fixes

The addon shows `Error: NetworkError when attempting to fetch resource.` and the Browser Console has lines like:

```
Cross-Origin Request Blocked: ... at https://mail-search.example.com/health.
(Reason: CORS request did not succeed). Status code: (null).
```

`Status code: (null)` means the TLS handshake never completed — there's no HTTP response for the browser to read CORS headers from. If your server cert is self-signed (or signed by a private CA) and Thunderbird doesn't trust the chain, every request fails this way.

You can confirm with `curl` from the same machine Thunderbird runs on:

```bash
curl -i https://mail-search.example.com/health
# curl: (60) SSL certificate problem: self-signed certificate in certificate chain
```

If you see error 60, the cert isn't trusted by the system. Thunderbird uses its own trust store (NSS), not the system's, so the next steps target Thunderbird specifically.

## Prerequisites

You need a copy of either:

- The **CA certificate** (`ca.crt`) that signed your server cert — recommended, since trusting the CA covers any future server certs you sign with it.
- Or the **server certificate** itself, as a one-off override.

Get it onto the Thunderbird machine, e.g.:

```bash
scp user@your-server:/etc/apache2/ssl/your-ca.crt ./your-ca.crt
```

The file should be PEM-formatted (starts with `-----BEGIN CERTIFICATE-----`). If you have a DER file, convert it:

```bash
openssl x509 -inform der -in your-ca.der -out your-ca.crt
```

## Step 1 — Verify the chain works before importing

This catches "imported but still broken" before you touch Thunderbird:

```bash
curl -i --cacert ./your-ca.crt https://mail-search.example.com/health \
  -H 'X-API-Key: <your-api-key>'
```

- **200 with JSON body** — chain is valid, hostname matches, you're good. Continue to Step 2.
- **`SSL certificate problem`** — the CA file you have doesn't sign the server cert, or an intermediate certificate is missing from the chain. Fix the server's `SSLCertificateFile` / `SSLCACertificateFile` configuration before continuing.
- **`subject alternative name does not match`** — see Step 1b.

### Step 1b — Confirm the server cert has the right Subject Alternative Name

Modern clients (Thunderbird, browsers, curl) **ignore the certificate's Common Name** for hostname validation. They require the hostname in the **Subject Alternative Name** (SAN) extension. If you generated your cert before this became standard practice, it may be missing.

On the server:

```bash
openssl x509 -in /etc/your-ssl-dir/server.crt -noout -ext subjectAltName
```

Expected output:

```
X509v3 Subject Alternative Name:
    DNS:mail-search.example.com
```

If `subjectAltName` is empty or missing, regenerate the server certificate with a SAN. Quick example for a CSR config:

```ini
# server.cnf
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = mail-search.example.com

[v3_req]
subjectAltName = @alt_names

[alt_names]
DNS.1 = mail-search.example.com
```

```bash
openssl req -new -key server.key -out server.csr -config server.cnf
openssl x509 -req -in server.csr -CA your-ca.crt -CAkey your-ca.key \
  -CAcreateserial -out server.crt -days 825 -extensions v3_req \
  -extfile server.cnf
```

Replace the cert in your reverse proxy config and reload it, then re-run the `curl --cacert` check.

## Step 2 — Import the CA into Thunderbird

1. Open Thunderbird.
2. **☰ menu → Settings**.
3. Scroll down to **Privacy & Security**.
4. Under **Certificates**, click **Manage Certificates**.
5. Go to the **Authorities** tab.
6. Click **Import…** and select `your-ca.crt`.
7. In the trust dialog, check **"This certificate can identify websites."** Click **OK**.
8. Restart Thunderbird (not strictly required, but clears any cached failed-connection state).

## Step 3 — Verify in the addon

1. Reload the addon (Add-ons Manager → toggle disable/enable, or restart Thunderbird).
2. Open the search panel and click **Manage**. Health dots should be green and accounts populated.
3. Run a search.

If anything still fails, open the Browser Console (`Ctrl+Shift+J`) and check the error message. `Status code: (null)` again means TLS is still failing — go back to Step 1 and verify the chain with curl. Anything else is a CORS or backend problem; see the [reverse proxy guide](/thunderbird-ai-search/guides/reverse-proxy/) and [troubleshooting](/thunderbird-ai-search/reference/troubleshooting/).

## Caveats

- **Per-profile, per-machine.** Each Thunderbird profile has its own NSS trust store. Repeat the import on each machine and each profile that needs to reach the server.
- **Profile reset wipes the trust.** Keep `your-ca.crt` somewhere you can find it again.
- **CA private key is high-value.** Anyone with access to it can sign certificates that Thunderbird will trust for *any* hostname, not just yours — this is the same threat model as any private CA. Keep `your-ca.key` root-only on the server, off backups that leave the network, and don't reuse it across unrelated services.
- **DNS must resolve correctly.** Your hostname (e.g. `mail-search.example.com`) needs to resolve to the server's VPN/LAN IP from the Thunderbird machine. If your VPN doesn't push DNS for it, add a hosts-file entry or a local DNS override.

## Alternative: skip HTTPS on a trusted LAN

If the only reason you set up HTTPS was for the addon, and the network between your Thunderbird machine and the server is fully trusted (VPN, LAN), you can simply point the addon at the FastAPI server's plain-HTTP port and skip the reverse proxy entirely:

In addon options, set **Server URL** to `http://<server-ip>:8342`. The API key still protects access. No certs to manage. The downside is that traffic is unencrypted on the wire — only acceptable on networks you trust.
