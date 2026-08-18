/**
 * PASay Cloudflare smoke Worker.
 *
 * Purpose: prove the GitHub/local -> Wrangler -> Cloudflare -> public HTTPS
 * chain works end-to-end WITHOUT touching any production Pasay code.
 *
 * Contract:
 *   GET /health  -> 200 text/plain "PASAY_CF_SMOKE_OK"
 *   GET /        -> 200 JSON {service, status, version}
 *   any other    -> 404 JSON
 *
 * No secrets, no env bindings, no KV/R2/D1. Strictly read-only edge probe.
 */
export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return new Response("PASAY_CF_SMOKE_OK", {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    if (url.pathname === "/") {
      return new Response(
        JSON.stringify({
          service: "pasay-cf-smoke",
          status: "ok",
          version: "0.1.0",
          note: "Isolated edge probe. No production traffic. See /health.",
        }),
        {
          status: 200,
          headers: { "content-type": "application/json; charset=utf-8" },
        }
      );
    }

    return new Response(
      JSON.stringify({ error: "not_found", path: url.pathname }),
      {
        status: 404,
        headers: { "content-type": "application/json; charset=utf-8" },
      }
    );
  },
};
