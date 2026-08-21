<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";

const { data, error, run } = useApi(() => api.get("/api/v1/clinical/roles"));
const items = () => asList(data.value);
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="临床工作台" sub="专科角色 / 内置知识库" :error="error" >
      <template #actions><button class="btn" @click="run">刷新</button></template>
    </PageHeader>
    <div class="grid md:grid-cols-2 lg:grid-cols-3 gap-3">
      <div v-for="(r, i) in items()" :key="i" class="panel p-4 reveal">
        <div class="flex items-center gap-2">
          <span class="glow-dot"></span>
          <span class="text-sm font-medium">{{ r.code || r.name || r.role }}</span>
        </div>
        <div class="text-xs mt-1" style="color: var(--ink-dim)">{{ r.description || r.specialty || r.prompt?.slice?.(0, 120) || "" }}</div>
      </div>
    </div>
    <div v-if="items().length === 0" class="panel p-6 text-sm text-center" style="color: var(--ink-dim)">暂无角色</div>
  </div>
</template>
