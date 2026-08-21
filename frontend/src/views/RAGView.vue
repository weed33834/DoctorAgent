<script setup>
import { useApi, flatten, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";

const { data: stats, run: r1 } = useApi(() => api.get("/api/v1/cache/stats").catch(() => null));
const { data: agentic, run: r2 } = useApi(() => api.get("/api/v1/rag/agentic").catch(() => null));
const refresh = () => { r1(); r2(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="RAG" sub="缓存统计 / Agentic 检索">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <KvTable v-if="stats" :rows="flatten(stats)" />
    <KvTable v-if="agentic" :rows="flatten(agentic)" />
  </div>
</template>
