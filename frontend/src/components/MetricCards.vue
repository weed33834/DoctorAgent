<script setup>
import { computed } from "vue";

const props = defineProps({
  data: { type: Object, default: () => ({}) },
});

// top-level scalar metrics only
const items = computed(() =>
  Object.entries(props.data).filter(([, v]) => typeof v !== "object")
);
</script>

<template>
  <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
    <div v-for="[k, v] in items" :key="k" class="metric reveal">
      <div class="text-[11px] uppercase tracking-wider" style="color: var(--ink-dim)">{{ k }}</div>
      <div class="text-xl font-semibold mono mt-1" style="color: var(--accent)">{{ v }}</div>
    </div>
  </div>
</template>
