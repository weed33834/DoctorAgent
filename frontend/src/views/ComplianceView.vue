<script setup>
import { useApi, flatten, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import MetricCards from "../components/MetricCards.vue";

const { data, error, run } = useApi(() => api.get("/api/v1/compliance/status"));

// extract boolean flags as badges
function flags() {
  if (!data.value) return [];
  return Object.entries(data.value).filter(([, v]) => typeof v === "boolean");
}
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="合规" sub="HIPAA / 等保 / 审计合规状态" :error="error" >
      <template #actions><button class="btn" @click="run">刷新</button></template>
    </PageHeader>
    <div v-if="flags().length" class="flex flex-wrap gap-2 reveal">
      <span v-for="[k, v] in flags()" :key="k" class="px-2.5 py-1 rounded-full text-xs border"
        :style="v ? 'background:rgba(45,212,191,.12);color:var(--accent);border-color:var(--line-strong)' : 'background:rgba(251,113,133,.12);color:var(--danger);border-color:var(--line)'">
        {{ k }} · {{ v ? "✓" : "✗" }}
      </span>
    </div>
    <KvTable v-if="data" :rows="flatten(data)" />
  </div>
</template>
