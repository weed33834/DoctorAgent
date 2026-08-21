<script setup>
import { useApi, flatten, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import DataTable from "../components/DataTable.vue";
import ApiAction from "../components/ApiAction.vue";

const { data: summary } = useApi(() => api.get("/api/v1/multimodal/summary").catch(() => null));
const { data: assets } = useApi(() => api.get("/api/v1/multimodal/assets").catch(() => null));
const { data: search } = useApi(() => api.get("/api/v1/multimodal/search").catch(() => null));
const refresh = () => { summary.run(); assets.run(); search.run(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="多模态" sub="音 / 图 / 文档资产">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <ApiAction title="添加资产" path="/api/v1/multimodal/assets" :fields="[{ name: 'name', label: '名称' }, { name: 'modality', label: '模态', default: 'document' }, { name: 'content', label: '内容', type: 'textarea' }]" />
    <KvTable v-if="summary" :rows="flatten(summary)" />
    <DataTable v-if="search" :rows="asList(search, ['results', 'items'])" empty="暂无搜索结果" />
    <DataTable v-if="assets" :rows="asList(assets, ['assets', 'items'])" empty="暂无资产" />
  </div>
</template>
