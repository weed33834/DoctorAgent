<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import DataTable from "../components/DataTable.vue";

const { data: redteam } = useApi(() => api.get("/api/v1/security/redteam").catch(() => null));
const { data: threats } = useApi(() => api.get("/api/v1/security/threat-cases").catch(() => null));
const refresh = () => { redteam.run(); threats.run(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="安全演练" sub="红队 / 威胁用例">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <DataTable v-if="redteam" :rows="asList(redteam, ['cases', 'items'])" empty="暂无演练" />
    <DataTable v-if="threats" :rows="asList(threats, ['cases', 'items'])" empty="暂无威胁用例" />
  </div>
</template>
