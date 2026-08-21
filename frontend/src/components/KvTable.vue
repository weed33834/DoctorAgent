<script setup>
defineProps({
  rows: { type: Array, default: () => [] },
});
</script>

<template>
  <div class="panel overflow-hidden">
    <div v-if="!rows.length" class="px-4 py-8 text-sm text-center" style="color: var(--ink-dim)">暂无数据</div>
    <table v-else class="tbl">
      <tbody>
        <tr v-for="r in rows" :key="r.k" class="last:border-0">
          <td class="w-2/5" style="color: var(--ink-dim)">{{ r.k }}</td>
          <td class="mono">{{ r.v }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
