<script setup>
import { useApi, flatten, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";

const { data: prefs, error, run } = useApi(() => api.get("/api/v1/rl/preferences").catch(() => null));
const { data: policy } = useApi(() => api.get("/api/v1/rl/policy").catch(() => null));
const refresh = () => { prefs.run(); policy.run(); };
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="强化学习" sub="偏好 / 策略" :error="error" >
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <KvTable v-if="prefs" :rows="flatten(prefs)" />
    <KvTable v-if="policy" :rows="flatten(policy)" />
  </div>
</template>
