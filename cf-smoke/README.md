# pasay-cf-smoke

Isolated Cloudflare Worker that proves the deployment chain
GitHub/local -> Wrangler -> Cloudflare -> public HTTPS works.

## Contract

| Path | Method | Response |
| --- | --- | --- |
| `/health` | GET | `200 text/plain "PASAY_CF_SMOKE_OK"` |
| `/` | GET | `200 application/json` with `{service, status, version, note}` |
| anything else | any | `404 application/json {"error":"not_found","path":...}` |

No secrets, no env bindings, no KV/R2/D1. Strictly a read-only edge probe.

## Local dev

```bash
cd cf-smoke
npm install --no-save wrangler@^4.123.0
npx wrangler dev
```

## Deploy

```bash
npx wrangler deploy -c cf-smoke/wrangler.toml --env=""
```

Production deploy requires `wrangler login` (Owner) or `CLOUDFLARE_API_TOKEN`
+ `CLOUDFLARE_ACCOUNT_ID` env vars.

## CI

`.github/workflows/cf-smoke-deploy.yml` runs the dry-run on every push to
`codex/pasay-cf-env-001` and the actual deploy on `workflow_dispatch` or
pushes to the same branch.
