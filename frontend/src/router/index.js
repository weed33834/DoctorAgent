// Route table for all 29 console views.
// Core views have dedicated components; the rest use a generic stub.
import StubView from "../views/StubView.vue";
import SystemView from "../views/SystemView.vue";
import SettingsView from "../views/SettingsView.vue";
import ConnectionsView from "../views/ConnectionsView.vue";
import ChatView from "../views/ChatView.vue";
import VaultView from "../views/VaultView.vue";
import EnterpriseView from "../views/EnterpriseView.vue";
import PromptsView from "../views/PromptsView.vue";
import HooksView from "../views/HooksView.vue";
import PluginsView from "../views/PluginsView.vue";
import CollabView from "../views/CollabView.vue";
import RAGView from "../views/RAGView.vue";
import DeidentifyView from "../views/DeidentifyView.vue";
import AgentsView from "../views/AgentsView.vue";
import MemoryView from "../views/MemoryView.vue";
import KGView from "../views/KGView.vue";
import DAGView from "../views/DAGView.vue";
import ComplianceView from "../views/ComplianceView.vue";
import SafetyView from "../views/SafetyView.vue";
import AuditView from "../views/AuditView.vue";
import OpsView from "../views/OpsView.vue";
import ClinicalView from "../views/ClinicalView.vue";
import EvalView from "../views/EvalView.vue";
import EvoView from "../views/EvoView.vue";
import ExpView from "../views/ExpView.vue";
import RLView from "../views/RLView.vue";
import AdminView from "../views/AdminView.vue";
import ConfigView from "../views/ConfigView.vue";
import KBView from "../views/KBView.vue";
import PipelineView from "../views/PipelineView.vue";
import GovernanceView from "../views/GovernanceView.vue";
import MultimodalView from "../views/MultimodalView.vue";
import InteropView from "../views/InteropView.vue";
import DRView from "../views/DRView.vue";
import CostView from "../views/CostView.vue";
import RedTeamView from "../views/RedTeamView.vue";
import SandboxView from "../views/SandboxView.vue";
import ScanView from "../views/ScanView.vue";

// Sidebar navigation groups.
export const navGroups = [
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

// view name -> (component, title)
const viewDefs = {
  system: [SystemView, "系统状态"],
  settings: [SettingsView, "设置"],
  connections: [ConnectionsView, "连接"],
  chat: [ChatView, "临床对话"],
  vault: [VaultView, "Vault"],
  enterprise: [EnterpriseView, "企业"],
  obs: [SystemView, "可观测性"],
  prompts: [PromptsView, "提示词"],
  hooks: [HooksView, "生命周期钩子"],
  plugins: [PluginsView, "插件"],
  collab: [CollabView, "协作"],
  rag: [RAGView, "RAG"],
  deidentify: [DeidentifyView, "去标识化"],
  agents: [AgentsView, "智能体"],
  mem: [MemoryView, "记忆"],
  kg: [KGView, "知识图谱"],
  dag: [DAGView, "DAG 编排"],
  compliance: [ComplianceView, "合规"],
  safety: [SafetyView, "安全护栏"],
  audit: [AuditView, "审计"],
  ops: [OpsView, "运维"],
  clinical: [ClinicalView, "临床工作台"],
  eval: [EvalView, "评测"],
  evo: [EvoView, "自进化"],
  exp: [ExpView, "经验库"],
  rl: [RLView, "强化学习"],
  tenants: [EnterpriseView, "企业 / 租户"],
  admin: [AdminView, "管理员"],
  config: [ConfigView, "配置"],
  kb: [KBView, "知识库"],
  pipeline: [PipelineView, "数据管道"],
  governance: [GovernanceView, "数据治理"],
  multimodal: [MultimodalView, "多模态"],
  interop: [InteropView, "互操作"],
  dr: [DRView, "灾难恢复"],
  cost: [CostView, "成本与定价"],
  redteam: [RedTeamView, "安全演练"],
  sandbox: [SandboxView, "沙箱"],
  scan: [ScanView, "安全扫描"],
};

function makeRoutes() {
  const routes = [];
  const titles = {};
  for (const g of navGroups)
    for (const it of g.items) titles[it.view] = it.name;

  for (const g of navGroups) {
    for (const it of g.items) {
      const [comp, t] = viewDefs[it.view] || [StubView, titles[it.view]];
      routes.push({
        path: `/${it.view}`,
        name: it.view,
        component: comp,
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
