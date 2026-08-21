<script setup>
import { ref } from "vue";
import { api } from "../api/client";

// Reusable "form → HTTP action" panel. One component covers any POST/PUT
// endpoint with a JSON body, so we never hand-write a per-endpoint form.
const props = defineProps({
  title: { type: String, required: true },
  path: { type: String, required: true },
  method: { type: String, default: "post" },
  // [{name, label, type: text|number|textarea|password, placeholder, default}]
  fields: { type: Array, default: () => [] },
  help: { type: String, default: "" },
});

const model = ref({});
props.fields.forEach((f) => (model.value[f.name] = f.default ?? ""));
const busy = ref(false);
const result = ref(null);
const error = ref("");

function reset() {
  props.fields.forEach((f) => (model.value[f.name] = f.default ?? ""));
}

async function run() {
  busy.value = true;
  error.value = "";
  result.value = null;
  try {
    const body = {};
    for (const f of props.fields) {
      const v = model.value[f.name];
      body[f.name] = f.type === "number" ? Number(v) : v;
    }
    result.value = await api[props.method](props.path, body);
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="panel p-4 space-y-3 reveal">
    <div class="flex items-center justify-between">
      <div class="text-sm font-semibold">{{ title }}</div>
      <button class="btn" @click="reset">重置</button>
    </div>
    <p v-if="help" class="text-xs" style="color: var(--ink-dim)">{{ help }}</p>
    <div class="grid md:grid-cols-2 gap-2">
      <input
        v-for="f in fields"
        :key="f.name"
        v-model="model[f.name]"
        :type="f.type === 'number' ? 'number' : f.type === 'password' ? 'password' : 'text'"
        :placeholder="f.placeholder || f.label"
        class="field"
      />
    </div>
    <textarea
      v-if="fields.some((f) => f.type === 'textarea')"
      v-for="f in fields.filter((x) => x.type === 'textarea')"
      :key="f.name"
      v-model="model[f.name]"
      rows="3"
      :placeholder="f.placeholder || f.label"
      class="field"
    ></textarea>
    <div class="flex items-center gap-3">
      <button class="btn btn-primary" :disabled="busy" @click="run">{{ busy ? "执行中…" : "执行" }}</button>
      <span v-if="error" class="text-sm" style="color: var(--danger)">{{ error }}</span>
    </div>
    <pre v-if="result" class="mono text-xs p-3 rounded-lg overflow-auto max-h-64" style="background: rgba(45,212,191,.05); border: 1px solid var(--line)">{{ typeof result === 'string' ? result : JSON.stringify(result, null, 2) }}</pre>
  </div>
</template>
