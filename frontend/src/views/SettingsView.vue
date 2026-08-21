<script setup>
import { ref, onMounted } from "vue";
import { getToken, setToken } from "../api/client";
import PageHeader from "../components/PageHeader.vue";

const dark = ref(true);
const token = ref("");
const saved = ref(false);

onMounted(() => {
  token.value = getToken();
  dark.value = localStorage.getItem("doctoragent_theme") !== "light";
});
function toggleTheme() {
  dark.value = !dark.value;
  document.documentElement.classList.toggle("dark", dark.value);
  document.documentElement.classList.toggle("light", !dark.value);
  localStorage.setItem("doctoragent_theme", dark.value ? "dark" : "light");
}
function saveToken() {
  setToken(token.value.trim());
  saved.value = true;
  setTimeout(() => (saved.value = false), 1500);
}
function clearToken() {
  setToken("");
  token.value = "";
}
</script>

<template>
  <div class="max-w-2xl space-y-4">
    <PageHeader title="设置" sub="外观 / API Token" />
    <div class="panel p-5 space-y-4 reveal">
      <div class="flex items-center justify-between">
        <div>
          <div class="text-sm font-medium">深色模式</div>
          <div class="text-xs mt-0.5" style="color: var(--ink-dim)">手术室仪表风格</div>
        </div>
        <button class="btn" @click="toggleTheme">{{ dark ? "切到浅色 ☀" : "切到深色 ☾" }}</button>
      </div>
      <div class="h-px" style="background: var(--line)"></div>
      <div class="space-y-2">
        <div class="text-sm font-medium">API Token</div>
        <div class="text-xs" style="color: var(--ink-dim)">仅保存在本次浏览器会话（sessionStorage），关闭标签页后清除。</div>
        <div class="flex gap-2">
          <input v-model="token" type="password" placeholder="Bearer token" class="field" />
          <button class="btn btn-primary" @click="saveToken">{{ saved ? "已保存 ✓" : "保存" }}</button>
          <button class="btn" @click="clearToken">清除</button>
        </div>
      </div>
    </div>
  </div>
</template>
