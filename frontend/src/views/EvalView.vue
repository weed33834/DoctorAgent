<script setup>
import { ref } from "vue";
import { api } from "../api/client";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import { flatten } from "../composables/useApi";

const result = ref(null);
const busy = ref(false);
const error = ref("");

async function run() {
  busy.value = true;
  error.value = "";
  result.value = null;
  try {
    result.value = await api.post("/api/v1/evaluate", { case_id: "demo", question: "评估示例病例", reference: "" });
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="评测" sub="DeepEval / 确定性回退指标" :error="error" >
      <template #actions>
        <button class="btn btn-primary" :disabled="busy" @click="run">{{ busy ? "评测中…" : "运行评测" }}</button>
      </template>
    </PageHeader>
    <KvTable v-if="result" :rows="flatten(result)" />
  </div>
</template>
