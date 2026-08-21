<script setup>
import { useApi, flatten, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import DataTable from "../components/DataTable.vue";
import ApiAction from "../components/ApiAction.vue";

const { data: cost } = useApi(() => api.get("/api/v1/cost/overview").catch(() => null));
const { data: daily } = useApi(() => api.get("/api/v1/cost/daily").catch(() => null));
const { data: models } = useApi(() => api.get("/api/v1/pricing/models").catch(() => null));
const refresh = () => { cost.run(); daily.run(); models.run(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="成本与定价" sub="模型成本 / 用量">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <div class="grid lg:grid-cols-2 gap-4">
      <ApiAction title="成本估算" path="/api/v1/pricing/estimate" :fields="[{ name: 'model', label: '模型' }, { name: 'tokens', label: 'Token 数', type: 'number', default: 1000 }]" />
      <ApiAction title="模型对比" path="/api/v1/pricing/compare" :fields="[{ name: 'models', label: '模型（逗号分隔）' }, { name: 'tokens', label: 'Token 数', type: 'number', default: 1000 }]" />
    </div>
    <KvTable v-if="cost" :rows="flatten(cost)" />
    <DataTable v-if="daily" :rows="asList(daily, ['daily', 'items'])" empty="暂无日用量" />
    <DataTable v-if="models" :rows="asList(models, ['models', 'items'])" empty="暂无模型" />
  </div>
</template>
