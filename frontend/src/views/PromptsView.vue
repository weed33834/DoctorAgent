<script setup>
import { ref } from "vue";
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import DataTable from "../components/DataTable.vue";
import ApiAction from "../components/ApiAction.vue";

const { data, error, run } = useApi(() => api.get("/api/v1/workspace/prompts"));
const { data: experts } = useApi(() => api.get("/api/v1/workspace/experts").catch(() => null));
const { data: skills } = useApi(() => api.get("/api/v1/workspace/skills").catch(() => null));
const items = () => asList(data.value);
const newContent = ref("");
const refresh = () => { run(); experts.run(); skills.run(); };

async function create() {
  if (!newContent.value.trim()) return;
  await api.post("/api/v1/workspace/prompts", { content: newContent.value.trim() });
  newContent.value = "";
  run();
}
async function del(it) {
  if (!confirm("删除该提示词？")) return;
  await api.delete(`/api/v1/workspace/prompts/${it.id || it.prompt_id}`);
  run();
}
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="提示词" sub="提示词 / 专家 / 技能" :error="error">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>

    <div class="panel p-4 space-y-2 reveal">
      <textarea v-model="newContent" rows="2" placeholder="新增提示词…" class="field"></textarea>
      <button class="btn btn-primary" @click="create">新增</button>
    </div>

    <div class="space-y-2">
      <div v-for="(it, i) in items()" :key="i" class="panel p-3 flex items-start justify-between gap-3 reveal">
        <div class="text-sm whitespace-pre-wrap">{{ it.content || it.text }}</div>
        <button @click="del(it)" class="text-xs shrink-0" style="color: var(--danger)">删除</button>
      </div>
      <div v-if="items().length === 0" class="panel p-6 text-sm text-center" style="color: var(--ink-dim)">暂无提示词</div>
    </div>

    <div class="grid lg:grid-cols-2 gap-4">
      <ApiAction title="新增专家" path="/api/v1/workspace/experts" :fields="[{ name: 'name', label: '名称' }, { name: 'description', label: '描述' }]" />
      <ApiAction title="新增技能" path="/api/v1/workspace/skills" :fields="[{ name: 'name', label: '技能名' }, { name: 'description', label: '描述' }]" />
    </div>
    <DataTable v-if="experts" :rows="asList(experts, ['experts', 'items'])" empty="暂无专家" />
    <DataTable v-if="skills" :rows="asList(skills, ['skills', 'items'])" empty="暂无技能" />
  </div>
</template>
