/** Tiny hash router — hash-based so the Mini App works without a server.
 *  Routes: #/, #/properties, #/work, #/finance, #/more, #/properties/:id,
 *          #/move-outs/:id
 */

export type Route =
  | { name: "home" }
  | { name: "properties" }
  | { name: "properties.detail"; propertyId: number }
  | { name: "work" }
  | { name: "finance" }
  | { name: "more" }
  | { name: "move_out.detail"; moveOutId: number };

export function parseHash(hash: string): Route {
  const cleaned = hash.replace(/^#/, "").replace(/^\//, "");
  if (cleaned.length === 0 || cleaned === "/") return { name: "home" };
  const parts = cleaned.split("/").filter(Boolean);
  if (parts[0] === "properties") {
    if (parts.length === 1) return { name: "properties" };
    const id = Number(parts[1]);
    if (Number.isFinite(id)) return { name: "properties.detail", propertyId: id };
  }
  if (parts[0] === "work") return { name: "work" };
  if (parts[0] === "finance") return { name: "finance" };
  if (parts[0] === "more") return { name: "more" };
  if (parts[0] === "move-outs") {
    const id = Number(parts[1]);
    if (Number.isFinite(id)) return { name: "move_out.detail", moveOutId: id };
  }
  return { name: "home" };
}

export function toHash(route: Route): string {
  switch (route.name) {
    case "home":
      return "#/";
    case "properties":
      return "#/properties";
    case "properties.detail":
      return `#/properties/${route.propertyId}`;
    case "work":
      return "#/work";
    case "finance":
      return "#/finance";
    case "more":
      return "#/more";
    case "move_out.detail":
      return `#/move-outs/${route.moveOutId}`;
  }
}

export type RouteListener = (route: Route) => void;
export class Router {
  current: Route = { name: "home" };
  listeners = new Set<RouteListener>();
  constructor() {
    this.current = parseHash(window.location.hash);
    window.addEventListener("hashchange", () => {
      this.current = parseHash(window.location.hash);
      this.listeners.forEach((fn) => fn(this.current));
    });
  }
  navigate(route: Route): void {
    const target = toHash(route);
    if (window.location.hash === target) return;
    window.location.hash = target;
  }
  subscribe(listener: RouteListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

export const router = new Router();
