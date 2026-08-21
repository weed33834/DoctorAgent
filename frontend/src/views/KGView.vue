<script setup>
import { ref } from "vue";
import { useApi, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";

const query = ref("");
const result = ref(null);
const { data: sub } = useApi(() => api.get("/api/v1/kg/subgraph?limit=50").catch(() => null));

async function runQuery() {
  if (!query.value.trim()) return;
  result.value = await api.post("/api/v1/kg/query", { query: query.value.trim() });
}
const nodes = () => (result.value || sub.value || {}).nodes || [];
const rels = () => {
  const r = result.value || sub.value || {};
  return r.relations || r.edges || [];
};
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="知识图谱" sub="实体 / 关系图谱">
      <template #actions>
        <input v-model="query" @keydown.enter="runQuery" placeholder="查询图谱…" class="field !w-56" />
        <button class="btn btn-primary" @click="runQuery">查询</button>
      </template>
    </PageHeader>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-2">
      <div v-for="(n, i) in nodes()" :key="n.id || i" class="panel px-3 py-2 text-sm reveal">
        {{ n.name || n.entity || n.id }}<span v-if="n.type" class="text-xs ml-1" style="color: var(--ink-dim)">({{ n.type }})</span>
      </div>
    </div>
    <div v-if="rels().length" class="panel p-4">
      <div class="text-xs font-semibold uppercase tracking-wider mb-2" style="color: var(--ink-dim)">关系</div>
      <div class="space-y-1 text-sm" style="color: var(--ink-dim)">
        <div v-for="(r, i) in rels()" :key="i" class="mono">{{ r.source || r.from }} —{{ r.relation || r.type || "" }}→ {{ r.target || r.to }}</div>
      </div>
    </div>
  </div>
</template>
