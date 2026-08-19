export interface WorkerHealthEnv {
  HYPERDRIVE?: unknown;
  PASAY_APP_ENV?: string;
  PASAY_HEALTHCHECK_DB_CONNECTIVITY?: string;
}

export interface WorkerHealthPayload {
  status: "ok" | "degraded";
  runtime: {
    platform: "cloudflare-workers";
    alive: true;
  };
  application: {
    boot: "ok";
    environment: string;
  };
  database: {
    configured: boolean;
    binding: "hyperdrive";
    connectivity_checked: boolean;
    reachable: null;
  };
}

export function isDatabaseConfigurationAvailable(env: WorkerHealthEnv): boolean {
  return env.HYPERDRIVE !== undefined && env.HYPERDRIVE !== null;
}

export function buildHealthPayload(env: WorkerHealthEnv): WorkerHealthPayload {
  const databaseConfigured = isDatabaseConfigurationAvailable(env);

  return {
    status: databaseConfigured ? "ok" : "degraded",
    runtime: {
      platform: "cloudflare-workers",
      alive: true,
    },
    application: {
      boot: "ok",
      environment: env.PASAY_APP_ENV || "local",
    },
    database: {
      configured: databaseConfigured,
      binding: "hyperdrive",
      connectivity_checked: env.PASAY_HEALTHCHECK_DB_CONNECTIVITY === "true",
      reachable: null,
    },
  };
}
