<script setup>
import { ref } from "vue";
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import DataTable from "../components/DataTable.vue";

const { data: skills, error, run } = useApi(() => api.get("/api/v1/agent/skills").catch(() => null));
const items = () => asList(skills.value);
const evolving = ref(false);
const result = ref("");

async function evolve() {
  evolving.value = true;
  result.value = "";
  try {
    const r = await api.post("/api/v1/agent/evolve", {});
    result.value = typeof r === "string" ? r : JSON.stringify(r);
  } catch (e) {
    result.value = `错误: ${e.message}`;
  } finally {
    evolving.value = false;
  }
}
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="自进化" sub="技能库 / 进化触发" :error="error" >
      <template #actions>
        <button class="btn" @click="run">刷新</button>
        <button class="btn btn-primary" :disabled="evolving" @click="evolve">{{ evolving ? "进化中…" : "触发进化" }}</button>
      </template>
    </PageHeader>
    <DataTable v-if="skills" :rows="items()" empty="暂无技能" />
    <div v-if="result" class="panel p-4">
      <div class="text-xs font-semibold uppercase tracking-wider mb-1" style="color: var(--accent)">进化结果</div>
      <div class="text-sm whitespace-pre-wrap mono">{{ result }}</div>
    </div>
  </div>
</template>
