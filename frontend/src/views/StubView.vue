<script setup>
defineProps({
  view: { type: String, default: "" },
  title: { type: String, default: "视图" },
});
</script>

<template>
  <div class="max-w-3xl">
    <h1 class="text-xl font-semibold mb-2">{{ title }}</h1>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">
      <code class="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800">/{{ view }}</code> 视图正在接入中。
    </p>
    <div class="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 p-10 text-center text-gray-400 dark:text-gray-500 text-sm">
      此视图将在后续迭代中实现。
    </div>
  </div>
</template>
