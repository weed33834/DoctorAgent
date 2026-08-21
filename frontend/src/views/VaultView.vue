<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import MetricCards from "../components/MetricCards.vue";
import DataTable from "../components/DataTable.vue";

const { data: status, run: refreshStatus } = useApi(() => api.get("/api/v1/vault/status").catch(() => null));
const { data: files, run: refreshFiles } = useApi(() => api.get("/api/v1/vault/files").catch(() => null));
const refresh = () => { refreshStatus(); refreshFiles(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="Vault" sub="加密文档库 / 检索索引">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <MetricCards v-if="status" :data="status" />
    <DataTable v-if="files" :rows="asList(files, ['files', 'items'])" empty="暂无文件" />
  </div>
</template>
