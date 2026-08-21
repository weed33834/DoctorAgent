<script setup>
import { useApi, flatten, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import DataTable from "../components/DataTable.vue";
import ApiAction from "../components/ApiAction.vue";

const { data: overview } = useApi(() => api.get("/api/v1/interop/overview").catch(() => null));
const { data: dir } = useApi(() => api.get("/api/v1/interop/directory").catch(() => null));
const { data: policies } = useApi(() => api.get("/api/v1/interop/policies").catch(() => null));
const { data: tasks } = useApi(() => api.get("/api/v1/interop/tasks").catch(() => null));
const refresh = () => { overview.run(); dir.run(); policies.run(); tasks.run(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="互操作" sub="外部 Agent / 目录 / 策略">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <ApiAction title="注册外部 Agent" path="/api/v1/interop/directory/register" :fields="[{ name: 'name', label: '名称' }, { name: 'url', label: 'Agent Card URL' }, { name: 'trust', label: '信任级别', default: 'limited' }]" />
    <KvTable v-if="overview" :rows="flatten(overview)" />
    <DataTable v-if="dir" :rows="asList(dir, ['directory', 'agents', 'items'])" empty="暂无目录条目" />
    <DataTable v-if="policies" :rows="asList(policies, ['policies', 'items'])" empty="暂无策略" />
    <DataTable v-if="tasks" :rows="asList(tasks, ['tasks', 'items'])" empty="暂无任务" />
  </div>
</template>
