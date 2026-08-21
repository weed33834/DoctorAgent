<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import DataTable from "../components/DataTable.vue";

const { data: sessions, run: r1 } = useApi(() => api.get("/api/v1/memory/sessions").catch(() => null));
const { data: facts, run: r2 } = useApi(() => api.get("/api/v1/memory/facts").catch(() => null));
const { data: episodes, run: r3 } = useApi(() => api.get("/api/v1/memory/episodes").catch(() => null));
const refresh = () => { r1(); r2(); r3(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="记忆" sub="会话 / 事实 / 片段">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <DataTable v-if="sessions" :rows="asList(sessions)" empty="暂无会话" />
    <DataTable v-if="facts" :rows="asList(facts)" empty="暂无事实" />
    <DataTable v-if="episodes" :rows="asList(episodes)" empty="暂无片段" />
  </div>
</template>
