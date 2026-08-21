<script setup>
import { useApi, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";

const { data, error, run } = useApi(() => api.get("/api/v1/agents/graph"));
const nodes = () => (data.value && data.value.nodes) || [];
const edges = () => (data.value && (data.value.edges || [])) || [];
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="智能体" sub="多智能体编排图" :error="error" >
      <template #actions><button class="btn" @click="run">刷新</button></template>
    </PageHeader>
    <div class="grid md:grid-cols-3 gap-3">
      <div v-for="(n, i) in nodes()" :key="n.id || i" class="panel p-4 reveal">
        <div class="flex items-center gap-2">
          <span class="glow-dot"></span>
          <span class="text-sm font-medium">{{ n.id || n.name || n.agent || "节点" }}</span>
        </div>
        <div class="text-xs mt-1" style="color: var(--ink-dim)">{{ n.role || n.description || n.type || "" }}</div>
      </div>
    </div>
    <div v-if="edges().length" class="panel p-4">
      <div class="text-xs font-semibold uppercase tracking-wider mb-2" style="color: var(--ink-dim)">边（{{ edges().length }}）</div>
      <div class="space-y-1 text-sm mono" style="color: var(--ink-dim)">
        <div v-for="(e, i) in edges()" :key="i">{{ e.source || e.from }} → {{ e.target || e.to }} <span v-if="e.type">({{ e.type }})</span></div>
      </div>
    </div>
  </div>
</template>
