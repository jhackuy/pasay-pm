import "./style.css";
import { apiGet } from "./api";

type Property = { id: number; name: string; address?: string };
type View = "home" | "properties" | "work" | "finance" | "more";

const app = document.querySelector<HTMLDivElement>("#app")!;
const labels: Record<View, string> = {
  home: "首页", properties: "房产", work: "工作", finance: "财务", more: "更多",
};

function shell(content: string, active: View) {
  app.innerHTML = `<main><header><p class="eyebrow">PASAY RENT</p><h1>${labels[active]}</h1></header>${content}</main>
    <nav aria-label="主导航">${(Object.keys(labels) as View[]).map(key =>
      `<button data-view="${key}" class="${key === active ? "active" : ""}">${labels[key]}</button>`
    ).join("")}</nav>`;
  document.querySelectorAll<HTMLButtonElement>("[data-view]").forEach(button => {
    button.onclick = () => render(button.dataset.view as View);
  });
}

async function render(view: View) {
  if (view === "home") {
    shell(`<section class="hero"><span>今日待办</span><strong>打开工作台处理下一步</strong><p>租金、维修与租约动作集中在同一处。</p></section>
      <section class="grid"><button data-view="work" class="card"><b>待处理事项</b><span>查看业务动作 →</span></button><button data-view="finance" class="card"><b>财务</b><span>租金与支出 →</span></button></section>`, view);
  } else if (view === "properties") {
    shell(`<section class="panel" id="property-list"><p class="muted">正在从 PASAY API 加载房产…</p></section>`, view);
    try {
      const result = await apiGet<Property[] | { items: Property[] }>("/properties");
      const items = Array.isArray(result) ? result : result.items;
      document.querySelector("#property-list")!.innerHTML = items.length
        ? items.map(item => `<article><b>${item.name}</b><span>${item.address ?? "查看房产详情"}</span></article>`).join("")
        : `<div class="empty"><b>还没有房产</b><span>从真实 PASAY API 创建第一处房产。</span></div>`;
    } catch {
      document.querySelector("#property-list")!.innerHTML = `<div class="error"><b>暂时无法连接服务</b><span>请检查网络后重试。</span><button id="retry">重试</button></div>`;
      document.querySelector<HTMLButtonElement>("#retry")!.onclick = () => render("properties");
    }
  } else {
    const copy = view === "work" ? ["业务工作台", "维修、续租、退租和待办动作将在这里汇总。"]
      : view === "finance" ? ["财务中心", "查看租金、付款凭证和支出状态。"]
      : ["设置与归档", "成员、语言偏好和完整活动记录。"];
    shell(`<section class="panel empty"><b>${copy[0]}</b><span>${copy[1]}</span></section>`, view);
  }
}

render("home");
