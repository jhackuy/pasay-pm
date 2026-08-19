import { describe, expect, it } from "vitest";

import { buildHealthPayload } from "../src/health";
import worker from "../src/index";

function makeEnv(overrides: Record<string, unknown> = {}) {
  return {
    HYPERDRIVE: {},
    PASAY_APP_ENV: "local",
    PASAY_HEALTHCHECK_DB_CONNECTIVITY: "false",
    ...overrides,
  };
}

describe("cloudflare worker health foundation", () => {
  it("builds an ok payload when Hyperdrive is configured", () => {
    expect(buildHealthPayload(makeEnv())).toEqual({
      status: "ok",
      runtime: {
        platform: "cloudflare-workers",
        alive: true,
      },
      application: {
        boot: "ok",
        environment: "local",
      },
      database: {
        configured: true,
        binding: "hyperdrive",
        connectivity_checked: false,
        reachable: null,
      },
    });
  });

  it("returns degraded when Hyperdrive is missing", async () => {
    const request = new Request("https://example.com/health");
    const response = await worker.fetch(request, makeEnv({ HYPERDRIVE: undefined }));

    expect(response.status).toBe(503);
    expect(response.headers.get("cache-control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual({
      status: "degraded",
      runtime: {
        platform: "cloudflare-workers",
        alive: true,
      },
      application: {
        boot: "ok",
        environment: "local",
      },
      database: {
        configured: false,
        binding: "hyperdrive",
        connectivity_checked: false,
        reachable: null,
      },
    });
  });
});
