<script setup>
import { useApi, flatten, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import DataTable from "../components/DataTable.vue";
import ApiAction from "../components/ApiAction.vue";

const { data: overview, run: r1 } = useApi(() => api.get("/api/v1/pipeline/overview").catch(() => null));
const { data: pipes, run: r2 } = useApi(() => api.get("/api/v1/pipeline/pipelines").catch(() => null));
const { data: sources, run: r3 } = useApi(() => api.get("/api/v1/pipeline/sources").catch(() => null));
const { data: runs, run: r4 } = useApi(() => api.get("/api/v1/pipeline/runs").catch(() => null));
const { data: quality, run: r5 } = useApi(() => api.get("/api/v1/pipeline/quality").catch(() => null));
const refresh = () => { r1(); r2(); r3(); r4(); r5(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="数据管道" sub="数据源 / 管道 / 运行 / 质量">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <div class="grid lg:grid-cols-2 gap-4">
      <ApiAction title="新建管道" path="/api/v1/pipeline/pipelines" :fields="[{ name: 'name', label: '管道名' }, { name: 'type', label: '类型', default: 'ingest' }]" />
      <ApiAction title="添加数据源" path="/api/v1/pipeline/sources" :fields="[{ name: 'name', label: '数据源名' }, { name: 'url', label: 'URL' }]" />
    </div>
    <KvTable v-if="overview" :rows="flatten(overview)" />
    <DataTable v-if="pipes" :rows="asList(pipes, ['pipelines', 'items'])" empty="暂无管道" />
    <DataTable v-if="sources" :rows="asList(sources, ['sources', 'items'])" empty="暂无数据源" />
    <DataTable v-if="runs" :rows="asList(runs, ['runs', 'items'])" empty="暂无运行" />
    <DataTable v-if="quality" :rows="asList(quality, ['quality', 'items'])" empty="暂无质量数据" />
  </div>
</template>
