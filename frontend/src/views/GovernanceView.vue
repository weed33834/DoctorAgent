<script setup>
import { useApi, flatten, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import DataTable from "../components/DataTable.vue";
import ApiAction from "../components/ApiAction.vue";

const { data: summary } = useApi(() => api.get("/api/v1/governance/summary").catch(() => null));
const { data: assets } = useApi(() => api.get("/api/v1/governance/assets").catch(() => null));
const refresh = () => { summary.run(); assets.run(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="数据治理" sub="资产 / 分类 / 血缘">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <div class="grid lg:grid-cols-2 gap-4">
      <ApiAction title="登记资产" path="/api/v1/governance/assets" :fields="[{ name: 'name', label: '资产名' }, { name: 'type', label: '类型', default: 'dataset' }, { name: 'owner', label: '负责人' }]" />
      <ApiAction title="血缘关系" path="/api/v1/governance/lineage" :fields="[{ name: 'source', label: '源资产' }, { name: 'target', label: '目标资产' }, { name: 'relation', label: '关系', default: 'depends_on' }]" />
    </div>
    <KvTable v-if="summary" :rows="flatten(summary)" />
    <DataTable v-if="assets" :rows="asList(assets, ['assets', 'items'])" empty="暂无资产" />
  </div>
</template>
