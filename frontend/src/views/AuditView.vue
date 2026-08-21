<script setup>
import { useApi, asList, flatten, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import MetricCards from "../components/MetricCards.vue";
import DataTable from "../components/DataTable.vue";

const { data: overview, run: r1 } = useApi(() => api.get("/api/v1/security/overview").catch(() => null));
const { data: events, run: r2 } = useApi(() => api.get("/api/v1/security/events").catch(() => null));
const refresh = () => { r1(); r2(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="审计" sub="HMAC 审计链 / 安全事件">
      <template #actions>
        <button class="btn" @click="refresh">刷新</button>
        <a class="btn" href="/api/v1/audit/export" download>导出 CSV</a>
      </template>
    </PageHeader>
    <MetricCards v-if="overview" :data="overview" />
    <DataTable v-if="events" :rows="asList(events, ['events', 'items'])" empty="暂无事件" />
  </div>
</template>
