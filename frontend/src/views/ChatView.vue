<script setup>
import { ref } from "vue";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { getToken } from "../api/client";
import PageHeader from "../components/PageHeader.vue";

const messages = ref([]);
const input = ref("");
const busy = ref(false);
const error = ref("");

function md(s) {
  try { return DOMPurify.sanitize(marked.parse(s)); } catch { return s; }
}
async function ask() {
  const q = input.value.trim();
  if (!q || busy.value) return;
  error.value = "";
  messages.value.push({ role: "user", text: q });
  messages.value.push({ role: "assistant", text: "", html: "" });
  input.value = "";
  busy.value = true;
  const last = messages.value[messages.value.length - 1];
  try {
    const headers = { "Content-Type": "application/json" };
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const res = await fetch("/vault/ask", { method: "POST", headers, body: JSON.stringify({ question: q, use_knowledge: true, web_search: false }) });
    if (!res.ok || !res.body) throw new Error(`请求失败 (${res.status})`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() || "";
      for (const part of parts) for (const line of part.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const p = line.slice(5).trim();
        if (p === "[DONE]") continue;
        try { const j = JSON.parse(p); last.text += j.content || j.text || ""; }
        catch { last.text += p; }
        last.html = md(last.text);
      }
    }
  } catch (e) {
    error.value = e.message;
    last.text += `\n[错误] ${e.message}`;
    last.html = md(last.text);
  } finally { busy.value = false; }
}
</script>

<template>
  <div class="flex flex-col h-full max-w-4xl mx-auto">
    <div class="mb-3"><PageHeader title="临床对话" sub="向 DoctorAgent 提问用药 / 风险 / 文献" /></div>
    <div class="flex-1 overflow-y-auto space-y-3 mb-3 min-h-64 panel p-4">
      <div v-if="!messages.length" class="text-sm text-center pt-10" style="color: var(--ink-dim)">
        <div class="glow-dot mx-auto mb-3"></div>
        例如：评估这位患者的用药方案
      </div>
      <div v-for="(m, i) in messages" :key="i" class="flex" :class="m.role === 'user' ? 'justify-end' : 'justify-start'">
        <div class="max-w-[82%] rounded-xl px-3 py-2 text-sm"
          :style="m.role === 'user'
            ? 'background:linear-gradient(135deg,#2dd4bf,#14b8a6);color:#04201c'
            : 'background:var(--surface-2);border:1px solid var(--line)'">
          <div v-if="m.role === 'assistant'" v-html="m.html"></div>
          <template v-else>{{ m.text }}</template>
        </div>
      </div>
      <div v-if="busy" class="text-xs" style="color: var(--ink-dim)">思考中…</div>
    </div>
    <div class="flex gap-2">
      <input v-model="input" @keydown.enter="ask" placeholder="输入问题，回车发送" class="field" />
      <button class="btn btn-primary" :disabled="busy" @click="ask">发送</button>
    </div>
  </div>
</template>
