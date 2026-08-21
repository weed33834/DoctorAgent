<script setup>
import { computed } from "vue";

const props = defineProps({
  rows: { type: Array, default: () => [] },
  empty: { type: String, default: "暂无数据" },
});

const cols = computed(() => {
  if (!props.rows.length) return [];
  return Object.keys(props.rows[0]).filter((k) => typeof props.rows[0][k] !== "object");
});
</script>

<template>
  <div class="panel overflow-hidden">
    <div v-if="!rows.length" class="px-4 py-8 text-sm text-center" style="color: var(--ink-dim)">{{ empty }}</div>
    <div v-else class="overflow-x-auto">
      <table class="tbl">
        <thead><tr><th v-for="c in cols" :key="c">{{ c }}</th></tr></thead>
        <tbody>
          <tr v-for="(row, i) in rows" :key="i">
            <td v-for="c in cols" :key="c" class="mono">{{ row[c] }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
