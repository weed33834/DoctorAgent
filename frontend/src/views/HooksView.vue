<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";

const { data, error, run } = useApi(() => api.get("/api/v1/hooks"));
const items = () => asList(data.value);
async function toggle(it) {
  await api.post(`/api/v1/hooks/${it.id}/toggle`, { enabled: !it.enabled });
  it.enabled = !it.enabled;
}
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="生命周期钩子" sub="事件 → 条件 → 动作" :error="error" >
      <template #actions><button class="btn" @click="run">刷新</button></template>
    </PageHeader>
    <div class="space-y-2">
      <div v-for="(it, i) in items()" :key="i" class="panel p-3 flex items-center justify-between reveal">
        <div>
          <div class="text-sm font-medium">{{ it.name || it.event || it.id }}</div>
          <div class="text-xs mono mt-0.5" style="color: var(--ink-dim)">{{ it.event || "" }} {{ it.condition || "" }}</div>
        </div>
        <label class="flex items-center gap-2 text-sm">
          <input type="checkbox" :checked="it.enabled" @change="toggle(it)" class="accent-teal-500" />
          <span style="color: var(--ink-dim)">启用</span>
        </label>
      </div>
      <div v-if="items().length === 0" class="panel p-6 text-sm text-center" style="color: var(--ink-dim)">暂无钩子</div>
    </div>
  </div>
</template>
