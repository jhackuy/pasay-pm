# Pasay Cloudflare Worker Foundation

This directory is the PASAY-ARCH-CF-NEON-001 foundation slice for the future
Cloudflare runtime. It intentionally exposes only a minimal `GET /health`
Worker and keeps all business logic in the existing Python codebase.

## Configuration model

- Worker runtime: Cloudflare Workers (`wrangler.jsonc`)
- Database edge boundary: Hyperdrive binding `HYPERDRIVE`
- Local dev credentials: use
  `CLOUDFLARE_HYPERDRIVE_LOCAL_CONNECTION_STRING_HYPERDRIVE`
- App-level non-secret vars: `.dev.vars` or `wrangler.jsonc` `vars`
- Secrets: manage outside the repo with Cloudflare secrets / Hyperdrive config

## Neon notes

- Application traffic should use the pooled Neon endpoint behind Hyperdrive.
- Migrations and admin tooling should use the direct Neon endpoint.
- This slice does not create or modify a second schema.

## Commands

- `npm install`
- `npm run typegen`
- `npm test`
- `npx wrangler dev`

For remote Hyperdrive behavior, use `npx wrangler dev --remote` only after the
real Hyperdrive binding ID is provisioned outside this repository.
