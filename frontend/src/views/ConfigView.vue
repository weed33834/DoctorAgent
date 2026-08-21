<script setup>
import { useApi, flatten, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";

const { data, error, run } = useApi(() => api.get("/api/v1/workspace/summary").catch(() => null));
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="配置" sub="工作区 / 提示词 / 技能 / 专家" :error="error" >
      <template #actions><button class="btn" @click="run">刷新</button></template>
    </PageHeader>
    <KvTable v-if="data" :rows="flatten(data)" />
  </div>
</template>
