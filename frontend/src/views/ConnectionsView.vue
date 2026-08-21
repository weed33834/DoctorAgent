<script setup>
import { ref } from "vue";
import { useApi, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";

const { data, error, run } = useApi(() => api.get("/connections"));
const items = () => data.value || [];
const form = ref({ name: "", base_url: "", auth: "" });

async function create() {
  if (!form.value.name.trim()) return;
  await api.post("/connections", { name: form.value.name.trim(), base_url: form.value.base_url.trim(), auth: form.value.auth.trim() });
  form.value = { name: "", base_url: "", auth: "" };
  run();
}
async function del(c) {
  if (!confirm(`删除连接「${c.name || c.id}」？`)) return;
  await api.delete(`/connections/${c.id}`);
  run();
}
async function test(c) {
  const r = await api.post(`/connections/${c.id}/test`);
  alert(r && r.ok ? "连接正常" : JSON.stringify(r));
}
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="连接" sub="模型 / 外部服务连接" :error="error" >
      <template #actions><button class="btn" @click="run">刷新</button></template>
    </PageHeader>

    <div class="panel p-4 space-y-3 reveal">
      <div class="grid md:grid-cols-3 gap-2">
        <input v-model="form.name" placeholder="名称" class="field" />
        <input v-model="form.base_url" placeholder="Base URL" class="field" />
        <input v-model="form.auth" placeholder="Auth（可选）" class="field" />
      </div>
      <button class="btn btn-primary" @click="create">新增连接</button>
    </div>

    <div class="panel overflow-hidden">
      <div v-if="!items().length" class="px-4 py-8 text-sm text-center" style="color: var(--ink-dim)">暂无连接</div>
      <table v-else class="tbl">
        <tbody>
          <tr v-for="c in items()" :key="c.id">
            <td class="px-3 py-2">{{ c.name }}</td>
            <td class="px-3 py-2 mono text-xs" style="color: var(--ink-dim)">{{ c.base_url }}</td>
            <td class="px-3 py-2 text-right space-x-3">
              <button class="text-xs" style="color: var(--accent)" @click="test(c)">测试</button>
              <button class="text-xs" style="color: var(--danger)" @click="del(c)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
