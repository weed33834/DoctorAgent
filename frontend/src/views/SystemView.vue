<script setup>
import { ref } from "vue";
import { useApi, flatten, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import MetricCards from "../components/MetricCards.vue";
import KvTable from "../components/KvTable.vue";

const { data: health, run: r1 } = useApi(() => api.get("/api/health").catch(() => null));
const { data: snap, run: r2 } = useApi(() => api.get("/api/v1/observability/snapshot").catch(() => null));
const metrics = ref("");
async function loadMetrics() {
  const r = await fetch("/api/v1/observability/metrics");
  metrics.value = r.ok ? await r.text() : "";
}
const refresh = () => { r1(); r2(); loadMetrics(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="系统状态" sub="健康 / OTel 快照 / Prometheus 指标">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <MetricCards v-if="health" :data="health" />
    <KvTable v-if="snap" :rows="flatten(snap)" />
    <div v-if="metrics" class="panel overflow-hidden">
      <div class="px-4 py-2 text-xs font-semibold uppercase tracking-wider" style="color: var(--ink-dim)">Prometheus</div>
      <pre class="mono p-4 text-xs overflow-auto max-h-96" style="color: var(--ink)">{{ metrics }}</pre>
    </div>
  </div>
</template>
