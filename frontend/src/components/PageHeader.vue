<script setup>
defineProps({
  title: { type: String, required: true },
  sub: { type: String, default: "" },
  error: { type: String, default: "" },
});
</script>

<template>
  <div class="reveal">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h1 class="text-2xl font-semibold tracking-tight">{{ title }}</h1>
        <p v-if="sub" class="text-sm mt-1" style="color: var(--ink-dim)">{{ sub }}</p>
      </div>
      <div class="flex items-center gap-2 shrink-0">
        <slot name="actions" />
      </div>
    </div>
    <p v-if="error" class="text-sm mt-2" style="color: var(--danger)">{{ error }}</p>
  </div>
</template>
