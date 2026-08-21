<script setup>
import { ref } from "vue";
import { api } from "../api/client";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import { flatten } from "../composables/useApi";

const running = ref(false);
const result = ref(null);
const error = ref("");

async function exec() {
  running.value = true;
  error.value = "";
  result.value = null;
  try {
    // Execute a minimal DAG, then fetch its detailed status by id.
    const r = await api.post("/api/v1/dag/execute", { name: "default", tasks: [] });
    if (r && r.dag_id) {
      result.value = await api.get(`/api/v1/dag/status/${r.dag_id}`);
    } else {
      result.value = r;
    }
  } catch (e) {
    error.value = e.message;
  } finally {
    running.value = false;
  }
}
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="DAG 编排" sub="有向无环图任务执行" :error="error">
      <template #actions>
        <button class="btn btn-primary" :disabled="running" @click="exec">{{ running ? "执行中…" : "执行 DAG" }}</button>
      </template>
    </PageHeader>
    <KvTable v-if="result" :rows="flatten(result)" />
  </div>
</template>
