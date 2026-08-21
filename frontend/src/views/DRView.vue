<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import DataTable from "../components/DataTable.vue";
import ApiAction from "../components/ApiAction.vue";

const { data: plans, run: r1 } = useApi(() => api.get("/api/v1/dr/plans").catch(() => null));
const { data: backups, run: r2 } = useApi(() => api.get("/api/v1/dr/backups").catch(() => null));
const { data: drills, run: r3 } = useApi(() => api.get("/api/v1/dr/drills").catch(() => null));
const { data: metrics, run: r4 } = useApi(() => api.get("/api/v1/dr/metrics").catch(() => null));
const refresh = () => { r1(); r2(); r3(); r4(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="灾难恢复" sub="备份 / 演练 / 恢复计划">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <div class="grid lg:grid-cols-2 gap-4">
      <ApiAction title="新建备份" path="/api/v1/dr/backups" :fields="[{ name: 'name', label: '备份名' }, { name: 'scope', label: '范围', default: 'full' }]" />
      <ApiAction title="创建演练" path="/api/v1/dr/drills" :fields="[{ name: 'name', label: '演练名' }, { name: 'scenario', label: '场景' }]" />
    </div>
    <DataTable v-if="metrics" :rows="asList(metrics, ['metrics', 'items'])" empty="暂无指标" />
    <DataTable v-if="plans" :rows="asList(plans, ['plans', 'items'])" empty="暂无计划" />
    <DataTable v-if="backups" :rows="asList(backups, ['backups', 'items'])" empty="暂无备份" />
    <DataTable v-if="drills" :rows="asList(drills, ['drills', 'items'])" empty="暂无演练" />
  </div>
</template>
