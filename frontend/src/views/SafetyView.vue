<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import DataTable from "../components/DataTable.vue";

const { data, error, run } = useApi(() => api.get("/api/v1/safety/rules"));
const { data: secRules } = useApi(() => api.get("/api/v1/security/rules").catch(() => null));
const items = () => asList(data.value);
const refresh = () => { run(); secRules.run(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="安全护栏" sub="确定性安全规则" :error="error">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <div class="grid md:grid-cols-2 gap-3">
      <div v-for="(r, i) in items()" :key="i" class="panel p-4 reveal">
        <div class="flex items-center justify-between">
          <span class="text-sm font-medium">{{ r.name || r.id || r.type || "规则" }}</span>
          <span class="text-[11px] px-2 py-0.5 rounded-full border"
            :style="r.enabled === false ? 'color:var(--danger);border-color:var(--line)' : 'color:var(--accent);border-color:var(--line-strong)'">
            {{ r.enabled === false ? "停用" : "启用" }}
          </span>
        </div>
        <div class="text-xs mt-1" style="color: var(--ink-dim)">{{ r.description || r.category || r.severity || "" }}</div>
      </div>
    </div>
    <div v-if="items().length === 0" class="panel p-6 text-sm text-center" style="color: var(--ink-dim)">暂无安全规则</div>
    <DataTable v-if="secRules" :rows="asList(secRules, ['rules', 'items'])" empty="暂无安全规则条目" />
  </div>
</template>
