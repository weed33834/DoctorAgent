<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import DataTable from "../components/DataTable.vue";

const { data, error, run } = useApi(() => api.get("/api/v1/experiments").catch(() => null));
const items = () => asList(data.value);
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="经验库 / 实验" sub="离线实验记录" :error="error" >
      <template #actions><button class="btn" @click="run">刷新</button></template>
    </PageHeader>
    <DataTable v-if="data" :rows="items()" empty="暂无实验" />
  </div>
</template>
