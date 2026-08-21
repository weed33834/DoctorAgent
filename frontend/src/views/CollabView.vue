<script setup>
import { useApi, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";

const { data: agents, run: r1 } = useApi(() => api.get("/api/v1/collab/agents").catch(() => null));
const { data: messages, run: r2 } = useApi(() => api.get("/api/v1/collab/messages").catch(() => null));
const refresh = () => { r1(); r2(); };
const agentList = () => asList(agents.value);
const msgList = () => asList(messages.value);
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="协作" sub="A2A 智能体 / 消息总线">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>    </PageHeader>
    <div class="grid md:grid-cols-3 gap-3">
      <div v-for="(a, i) in agentList()" :key="i" class="panel p-4 reveal">
        <div class="flex items-center gap-2"><span class="glow-dot"></span><span class="text-sm font-medium">{{ a.name || a.id }}</span></div>
        <div class="text-xs mt-1" style="color: var(--ink-dim)">{{ a.card?.description || a.description || a.status || "" }}</div>
      </div>
    </div>
    <div v-if="msgList().length" class="panel p-4">
      <div class="text-xs font-semibold uppercase tracking-wider mb-2" style="color: var(--ink-dim)">最近消息</div>
      <div class="divide-y" style="border-color: var(--line)">
        <div v-for="(m, i) in msgList().slice(0, 20)" :key="i" class="py-2 text-sm">
          <span class="text-xs mono" style="color: var(--ink-dim)">{{ m.sender }} → {{ m.recipient }} · {{ m.topic }}</span>
          <div class="mt-0.5">{{ typeof m.content === 'string' ? m.content : JSON.stringify(m.content) }}</div>
        </div>
      </div>
    </div>
  </div>
</template>
