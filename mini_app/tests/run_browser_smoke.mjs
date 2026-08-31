// Orchestrator: spawns the Python harness, waits for the server URL,
// runs the Playwright browser smoke, and tears down the harness.
//
// This is the single command invoked by `npm run test:browser` from the
// `build-core-smoke` CI job. It deliberately uses only Node built-ins
// (`node:child_process`, `node:readline`) so it has zero extra deps
// beyond `playwright` itself.
//
// Why spawn the harness from Node instead of from the smoke?
// - We need a hard lifecycle contract: stdout line `READY_URL=...` is the
//   signal that the server is accepting requests.
// - The harness writes the SQLite file under a deterministic tempfile
//   path and deletes it on shutdown, so no DB leakage across CI runs.
// - On any Playwright failure, this orchestrator terminates the harness
//   with SIGTERM so the CI job does not hang.

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST = process.env.MINIAAPP_DIST
  ? resolve(process.env.MINIAAPP_DIST)
  : resolve(HERE, "..", "dist");
const HARNESS = resolve(HERE, "serve_app.py");
const SMOKE = resolve(HERE, "browser_smoke.mjs");

if (!existsSync(DIST) || !existsSync(resolve(DIST, "index.html"))) {
  console.error(
    `FAIL: Mini App dist/ not found at ${DIST}. Run 'npm run build' first.`,
  );
  process.exit(2);
}
if (!existsSync(HARNESS)) {
  console.error(`FAIL: harness not found at ${HARNESS}`);
  process.exit(2);
}

const harness = spawn("python3", [HARNESS, DIST], {
  stdio: ["ignore", "pipe", "pipe"],
  env: { ...process.env, PYTHONUNBUFFERED: "1" },
});

let serverUrl = null;
let resolved = false;
const ready = new Promise((resolveReady, rejectReady) => {
  const rl = createInterface({ input: harness.stdout });
  rl.on("line", (line) => {
    process.stdout.write(`[harness] ${line}\n`);
    if (line.startsWith("READY_URL=") && !resolved) {
      serverUrl = line.slice("READY_URL=".length).trim();
      resolved = true;
      resolveReady(serverUrl);
    }
  });
  harness.stderr.on("data", (chunk) => {
    process.stderr.write(`[harness:err] ${chunk}`);
  });
  harness.on("exit", (code, signal) => {
    if (!resolved) {
      rejectReady(new Error(`harness exited before READY_URL (code=${code} signal=${signal})`));
    }
  });
});

let smokeCode = 1;
let smokeError = null;
try {
  serverUrl = await Promise.race([
    ready,
    new Promise((_, reject) => setTimeout(() => reject(new Error("harness startup timed out")), 20000)),
  ]);
  console.log(`\n[orchestrator] server ready at ${serverUrl}\n`);

  smokeCode = await new Promise((resolveSmoke) => {
    const child = spawn(
      "node",
      [SMOKE],
      {
        stdio: "inherit",
        env: { ...process.env, BASE_URL: serverUrl },
      },
    );
    child.on("exit", (code) => resolveSmoke(code ?? 1));
  });
} catch (err) {
  smokeError = err;
  console.error(`\n[orchestrator] error: ${err.message ?? String(err)}`);
} finally {
  if (harness.exitCode === null) {
    try {
      harness.kill("SIGTERM");
    } catch (_) {
      // ignore — process already exiting
    }
    await new Promise((resolveExit) => {
      const timer = setTimeout(() => resolveExit(), 3000);
      harness.on("exit", () => {
        clearTimeout(timer);
        resolveExit();
      });
    });
  }
}

if (smokeError) {
  process.exit(1);
}
process.exit(smokeCode === 0 ? 0 : 1);
