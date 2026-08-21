<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";

const { data, error, run } = useApi(() => api.get("/api/v1/plugins"));
const items = () => asList(data.value);
async function toggle(it) {
  await api.post(`/api/v1/plugins/${it.id || it.name}/toggle`, { enabled: !it.enabled });
  it.enabled = !it.enabled;
}
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="插件" sub="MCP / 工具插件扩展" :error="error" >
      <template #actions><button class="btn" @click="run">刷新</button></template>
    </PageHeader>
    <div class="grid md:grid-cols-2 gap-3">
      <div v-for="(it, i) in items()" :key="i" class="panel p-4 flex items-center justify-between reveal">
        <div>
          <div class="text-sm font-medium">{{ it.name || it.id }}</div>
          <div class="text-xs mt-0.5" style="color: var(--ink-dim)">{{ it.version || it.description || "" }}</div>
        </div>
        <label class="flex items-center gap-2 text-sm">
          <input type="checkbox" :checked="it.enabled" @change="toggle(it)" class="accent-teal-500" />
        </label>
      </div>
    </div>
    <div v-if="items().length === 0" class="panel p-6 text-sm text-center" style="color: var(--ink-dim)">暂无插件</div>
  </div>
</template>
