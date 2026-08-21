<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import DataTable from "../components/DataTable.vue";

const { data, error, run } = useApi(() => api.get("/admin/roles").catch(() => null));
const items = () => asList(data.value, ["roles", "items"]);
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="管理员" sub="RBAC 角色（需 admin 权限）" :error="error" >
      <template #actions><button class="btn" @click="run">刷新</button></template>
    </PageHeader>
    <DataTable v-if="data" :rows="items()" empty="暂无角色" />
  </div>
</template>
