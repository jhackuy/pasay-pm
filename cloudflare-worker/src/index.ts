import { buildHealthPayload, type WorkerHealthEnv } from "./health";

export default {
  async fetch(request: Request, env: WorkerHealthEnv): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      const payload = buildHealthPayload(env);
      const status = payload.status === "ok" ? 200 : 503;
      return Response.json(payload, {
        status,
        headers: {
          "cache-control": "no-store",
        },
      });
    }

    return new Response("Not found", { status: 404 });
  },
};
