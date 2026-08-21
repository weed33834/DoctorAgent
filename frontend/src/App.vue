<script setup>
import { ref, computed, onMounted } from "vue";
import { useRoute } from "vue-router";
import { navGroups } from "./router";
import { getToken, setToken } from "./api/client";

const route = useRoute();
const dark = ref(true);
const tokenInput = ref(getToken());
const tokenSaved = ref(false);
const menuOpen = ref(false);

function applyTheme() {
  document.documentElement.classList.toggle("dark", dark.value);
  document.documentElement.classList.toggle("light", !dark.value);
}
function toggleTheme() {
  dark.value = !dark.value;
  applyTheme();
  localStorage.setItem("doctoragent_theme", dark.value ? "dark" : "light");
}
function saveToken() {
  setToken(tokenInput.value.trim());
  tokenSaved.value = true;
  setTimeout(() => (tokenSaved.value = false), 1500);
}
onMounted(() => {
  dark.value = localStorage.getItem("doctoragent_theme") !== "light";
  applyTheme();
});
const current = computed(() => route.meta.title || "控制台");
</script>

<template>
  <div class="flex h-screen overflow-hidden">
    <!-- Sidebar (glass) -->
    <aside class="w-64 shrink-0 hidden md:flex flex-col" :class="menuOpen ? 'flex' : ''">
      <div class="p-5 pb-4 border-b" style="border-color: var(--line)">
        <div class="flex items-center gap-2">
          <span class="glow-dot"></span>
          <span class="font-bold text-lg tracking-tight">DoctorAgent</span>
        </div>
        <div class="text-[11px] mt-1 mono" style="color: var(--ink-dim)">CLINICAL · v0.4</div>
      </div>
      <nav class="flex-1 overflow-y-auto p-3 space-y-4">
        <div v-for="g in navGroups" :key="g.label">
          <div class="px-2 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-[0.15em]" style="color: var(--ink-dim)">
            {{ g.label }}
          </div>
          <div class="space-y-0.5">
            <router-link
              v-for="it in g.items" :key="it.view" :to="`/${it.view}`"
              class="flex items-center gap-2 px-3 py-1.5 rounded-r-lg text-sm border-l-2 border-transparent transition-all"
              :class="route.path === '/' + it.view ? 'nav-active font-medium' : 'opacity-80 hover:opacity-100'"
              style="color: var(--ink)"
            >
              {{ it.name }}
            </router-link>
          </div>
        </div>
      </nav>
    </aside>

    <!-- Main -->
    <div class="flex-1 flex flex-col min-w-0">
      <header class="h-14 shrink-0 flex items-center justify-between px-4 border-b" style="border-color: var(--line); background: var(--surface)">
        <div class="flex items-center gap-3">
          <span class="glow-dot"></span>
          <span class="text-sm" style="color: var(--ink-dim)">{{ current }}</span>
        </div>
        <div class="flex items-center gap-2">
          <button @click="toggleTheme" class="btn !px-2.5">{{ dark ? "☀" : "☾" }}</button>
        </div>
      </header>

      <main class="flex-1 overflow-y-auto p-4 md:p-6 relative z-10">
        <router-view />
      </main>
    </div>

    <!-- Token gate -->
    <div v-if="!getToken()" class="fixed inset-0 z-50 flex items-center justify-center backdrop-blur-sm bg-black/40">
      <div class="panel w-96 p-6 reveal">
        <div class="flex items-center gap-2 mb-2">
          <span class="glow-dot"></span>
          <h2 class="text-lg font-semibold">接入认证</h2>
        </div>
        <p class="text-sm mb-4" style="color: var(--ink-dim)">
          输入 API Token 以访问受保护的 DoctorAgent API；未配置 token 时可直接留空（本地访问）。
        </p>
        <input v-model="tokenInput" type="password" placeholder="Bearer token（可选）" class="field mb-4" />
        <div class="flex justify-end">
          <button @click="saveToken" class="btn btn-primary">{{ tokenSaved ? "已保存 ✓" : "进入" }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
