# Optional Caddy site fragments

`Caddyfile` ends with `import /etc/caddy/conf.d/*.caddy`. A glob that matches
nothing is not an error, so this directory being empty is the supported
default.

## Enabling previews

🔴 The preview site used to live in `Caddyfile` unconditionally, and previews
are OFF by default. With `COMRADE_PREVIEW_DOMAIN` unset the site address became
`*.` and the `tls` block became `dns` with no arguments, so the real
`caddy:2-alpine` image refused the whole file:

```
Error: adapting config using caddyfile: parsing caddyfile tokens for 'tls':
wrong argument count or unexpected line ending after 'dns'
```

Caddy then exits, the public site is down, and nothing notices — the release
readiness check in `scripts/deploy_host.sh` talked to the API container
directly and never through the proxy. A deployment that had not enabled
previews could not serve its app.

To turn previews on, copy the fragment in and supply all three variables:

```bash
cp docker/caddy-previews.caddy docker/caddy.conf.d/previews.caddy
```

```
COMRADE_PREVIEW_DOMAIN=previews.example.com
COMRADE_DNS_PROVIDER=cloudflare        # the module built into your image
COMRADE_DNS_TOKEN=...
```

**The stock `caddy:2-alpine` image has no DNS provider module.** A wildcard
certificate is issued over DNS, so the fragment cannot work on the stock image
however the variables are set — build a Caddy image with the provider you use
(`caddy:2-builder` plus `xcaddy build --with github.com/caddy-dns/<provider>`)
and point the `caddy` service at it. `scripts/deploy_host.sh` validates the
rendered configuration against the image it is about to run, so an incomplete
setup fails before it replaces a working stack rather than after.
