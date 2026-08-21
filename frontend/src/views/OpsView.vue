<script setup>
import { useApi, asList, flatten, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import DataTable from "../components/DataTable.vue";

const { data: overview, error, run } = useApi(() =>
  Promise.all([
    api.get("/api/v1/analytics/overview").catch(() => null),
    api.get("/api/v1/stats/overview").catch(() => null),
  ]).then(([a, s]) => a || s)
);
const { data: tasks } = useApi(() => api.get("/api/v1/tasks").catch(() => null));
const { data: errors } = useApi(() => api.get("/api/v1/errors").catch(() => null));
const { data: taskSummary } = useApi(() => api.get("/api/v1/tasks/summary").catch(() => null));
const refresh = () => { run(); tasks.run(); errors.run(); taskSummary.run(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="运维" sub="平台分析 / 任务队列 / 错误" :error="error">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <KvTable v-if="overview" :rows="flatten(overview)" />
    <KvTable v-if="taskSummary" :rows="flatten(taskSummary)" />
    <DataTable v-if="tasks" :rows="asList(tasks)" empty="暂无任务" />
    <DataTable v-if="errors" :rows="asList(errors, ['errors', 'items'])" empty="暂无错误" />
  </div>
</template>
