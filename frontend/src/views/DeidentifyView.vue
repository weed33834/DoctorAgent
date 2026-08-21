<script setup>
import { ref } from "vue";
import { api } from "../api/client";
import PageHeader from "../components/PageHeader.vue";

const input = ref("");
const result = ref("");
const busy = ref(false);
const error = ref("");

async function run() {
  const t = input.value.trim();
  if (!t || busy.value) return;
  busy.value = true;
  error.value = "";
  result.value = "";
  try {
    const r = await api.post("/api/v1/deidentify", { text: t });
    result.value =
      typeof r === "string" ? r
      : r?.masked_text ?? r?.text ?? JSON.stringify(r, null, 2);
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="去标识化" sub="对 PHI（姓名 / 电话 / 身份证 / 地址）脱敏" :error="error" />
    <div class="panel p-5 space-y-3 reveal">
      <textarea v-model="input" rows="5" placeholder="粘贴含患者信息的文本…" class="field"></textarea>
      <button class="btn btn-primary" :disabled="busy" @click="run">{{ busy ? "处理中…" : "去标识化" }}</button>
      <div v-if="result" class="rounded-lg p-4" style="background: rgba(45,212,191,.06); border: 1px solid var(--line)">
        <div class="text-xs font-semibold uppercase tracking-wider mb-1" style="color: var(--accent)">脱敏结果</div>
        <div class="text-sm whitespace-pre-wrap">{{ result }}</div>
      </div>
    </div>
  </div>
</template>
