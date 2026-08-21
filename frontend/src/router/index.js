// Route table for the console.
//
// Structure:
//   * `allViewGroups` — every registered view, grouped for documentation.
//     Routes are generated from this so deep links keep working even when a
//     view is hidden from the sidebar.
//   * `navGroups` — what the sidebar actually renders. Only views with real
//     interactive workflows are listed; API-browser-style views remain
//     reachable by URL but are not advertised as product features.
//
// All view components are lazy-loaded (dynamic import) so each view ships
// as its own chunk instead of one monolithic bundle.

// Full view registry (grouped).
const allViewGroups = [
  {
    label: "工作台",
    items: [
      { view: "chat", name: "临床对话" },
      { view: "vault", name: "Vault" },
      { view: "clinical", name: "临床工作台" },
      { view: "deidentify", name: "去标识化" },
    ],
  },
  {
    label: "智能体",
    items: [
      { view: "agents", name: "智能体" },
      { view: "dag", name: "DAG 编排" },
      { view: "hooks", name: "生命周期钩子" },
      { view: "collab", name: "协作" },
      { view: "plugins", name: "插件" },
      { view: "prompts", name: "提示词" },
    ],
  },
  {
    label: "检索与知识",
    items: [
      { view: "rag", name: "RAG" },
      { view: "kg", name: "知识图谱" },
      { view: "mem", name: "记忆" },
      { view: "eval", name: "评测" },
      { view: "evo", name: "自进化" },
      { view: "exp", name: "经验库" },
      { view: "rl", name: "强化学习" },
    ],
  },
  {
    label: "安全与合规",
    items: [
      { view: "safety", name: "安全护栏" },
      { view: "compliance", name: "合规" },
      { view: "audit", name: "审计" },
      { view: "tenants", name: "租户" },
      { view: "admin", name: "管理员" },
    ],
  },
  {
    label: "系统",
    items: [
      { view: "system", name: "系统状态" },
      { view: "ops", name: "运维" },
      { view: "config", name: "配置" },
      { view: "connections", name: "连接" },
      { view: "enterprise", name: "企业 / 租户" },
      { view: "settings", name: "设置" },
    ],
  },
  {
    label: "数据与治理",
    items: [
      { view: "kb", name: "知识库" },
      { view: "pipeline", name: "数据管道" },
      { view: "governance", name: "数据治理" },
      { view: "multimodal", name: "多模态" },
      { view: "interop", name: "互操作" },
      { view: "dr", name: "灾难恢复" },
      { view: "cost", name: "成本与定价" },
      { view: "redteam", name: "安全演练" },
      { view: "sandbox", name: "沙箱" },
      { view: "scan", name: "安全扫描" },
    ],
  },
];

// Sidebar: production-ready views only. Everything else stays routable via
// deep link but is not presented as a finished feature (honesty > fullness).
export const navGroups = [
  {
    label: "临床",
    items: [
      { view: "chat", name: "临床对话" },
      { view: "clinical", name: "临床工作台" },
      { view: "vault", name: "文献库 Vault" },
      { view: "deidentify", name: "PHI 去标识化" },
    ],
  },
  {
    label: "系统",
    items: [
      { view: "audit", name: "审计链" },
      { view: "connections", name: "模型连接" },
      { view: "system", name: "系统状态" },
      { view: "settings", name: "设置" },
    ],
  },
];

// Lazy view loaders + titles.
const viewDefs = {
  system: [() => import("../views/SystemView.vue"), "系统状态"],
  settings: [() => import("../views/SettingsView.vue"), "设置"],
  connections: [() => import("../views/ConnectionsView.vue"), "连接"],
  chat: [() => import("../views/ChatView.vue"), "临床对话"],
  vault: [() => import("../views/VaultView.vue"), "Vault"],
  enterprise: [() => import("../views/EnterpriseView.vue"), "企业"],
  obs: [() => import("../views/SystemView.vue"), "可观测性"],
  prompts: [() => import("../views/PromptsView.vue"), "提示词"],
  hooks: [() => import("../views/HooksView.vue"), "生命周期钩子"],
  plugins: [() => import("../views/PluginsView.vue"), "插件"],
  collab: [() => import("../views/CollabView.vue"), "协作"],
  rag: [() => import("../views/RAGView.vue"), "RAG"],
  deidentify: [() => import("../views/DeidentifyView.vue"), "去标识化"],
  agents: [() => import("../views/AgentsView.vue"), "智能体"],
  mem: [() => import("../views/MemoryView.vue"), "记忆"],
  kg: [() => import("../views/KGView.vue"), "知识图谱"],
  dag: [() => import("../views/DAGView.vue"), "DAG 编排"],
  compliance: [() => import("../views/ComplianceView.vue"), "合规"],
  safety: [() => import("../views/SafetyView.vue"), "安全护栏"],
  audit: [() => import("../views/AuditView.vue"), "审计"],
  ops: [() => import("../views/OpsView.vue"), "运维"],
  clinical: [() => import("../views/ClinicalView.vue"), "临床工作台"],
  eval: [() => import("../views/EvalView.vue"), "评测"],
  evo: [() => import("../views/EvoView.vue"), "自进化"],
  exp: [() => import("../views/ExpView.vue"), "经验库"],
  rl: [() => import("../views/RLView.vue"), "强化学习"],
  tenants: [() => import("../views/EnterpriseView.vue"), "企业 / 租户"],
  admin: [() => import("../views/AdminView.vue"), "管理员"],
  config: [() => import("../views/ConfigView.vue"), "配置"],
  kb: [() => import("../views/KBView.vue"), "知识库"],
  pipeline: [() => import("../views/PipelineView.vue"), "数据管道"],
  governance: [() => import("../views/GovernanceView.vue"), "数据治理"],
  multimodal: [() => import("../views/MultimodalView.vue"), "多模态"],
  interop: [() => import("../views/InteropView.vue"), "互操作"],
  dr: [() => import("../views/DRView.vue"), "灾难恢复"],
  cost: [() => import("../views/CostView.vue"), "成本与定价"],
  redteam: [() => import("../views/RedTeamView.vue"), "安全演练"],
  sandbox: [() => import("../views/SandboxView.vue"), "沙箱"],
  scan: [() => import("../views/ScanView.vue"), "安全扫描"],
};

function makeRoutes() {
  const routes = [];
  const titles = {};
  for (const g of allViewGroups)
    for (const it of g.items) titles[it.view] = it.name;

  for (const g of allViewGroups) {
    for (const it of g.items) {
      const [comp, t] = viewDefs[it.view] || [null, titles[it.view]];
      routes.push({
        path: `/${it.view}`,
        name: it.view,
        component:
          comp ||
          (() => import("../views/StubView.vue")),
        props: { view: it.view, title: t },
        meta: { title: t },
      });
    }
  }
  routes.push({ path: "/", redirect: "/chat" });
  routes.push({ path: "/:pathMatch(.*)*", redirect: "/chat" });
  return routes;
}

export const routes = makeRoutes();
