<script setup>
import { useApi, flatten, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import DataTable from "../components/DataTable.vue";
import ApiAction from "../components/ApiAction.vue";

const { data: summary } = useApi(() => api.get("/api/v1/kb/summary").catch(() => null));
const { data: list } = useApi(() => api.get("/api/v1/kb").catch(() => null));
const { data: knowledge } = useApi(() => api.get("/api/v1/knowledge").catch(() => null));
const refresh = () => { summary.run(); list.run(); knowledge.run(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="知识库" sub="临床知识库 / 文档注册">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <ApiAction title="新建知识库" path="/api/v1/kb" :fields="[{ name: 'name', label: '名称' }, { name: 'description', label: '描述' }, { name: 'dir_name', label: '目录名', default: 'kb-1' }]" />
    <KvTable v-if="summary" :rows="flatten(summary)" />
    <DataTable v-if="list" :rows="asList(list)" empty="暂无知识库" />
    <DataTable v-if="knowledge" :rows="asList(knowledge, ['knowledge', 'items'])" empty="暂无知识条目" />
  </div>
</template>
