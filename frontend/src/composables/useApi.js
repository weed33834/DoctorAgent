import { ref } from "vue";
import { api } from "../api/client";

// Shared async-fetch pattern: { data, loading, error, run }.
// ``data``/``loading``/``error`` are refs (auto-unwrap when destructured and
// used as top-level names in templates).
export function useApi(loader, { immediate = true } = {}) {
  const data = ref(null);
  const loading = ref(false);
  const error = ref("");

  async function run(...args) {
    loading.value = true;
    error.value = "";
    try {
      data.value = await loader(...args);
      return data.value;
    } catch (e) {
      error.value = e.message;
      return null;
    } finally {
      loading.value = false;
    }
  }

  if (immediate) run();
  return { data, loading, error, run };
}

// Flatten a nested object into [{k, v}] rows for KvTable.
export function flatten(o, prefix = "") {
  if (!o || typeof o !== "object") return [];
  const out = [];
  for (const [k, v] of Object.entries(o)) {
    if (v && typeof v === "object" && !Array.isArray(v)) out.push(...flatten(v, `${prefix}${k}.`));
    else out.push({ k: `${prefix}${k}`, v });
  }
  return out;
}

// Normalise a list endpoint response into an array.
export function asList(r, keys = ["items", "files", "rules", "tasks", "events", "hooks", "agents", "nodes", "prompts", "sessions", "facts", "episodes"]) {
  if (Array.isArray(r)) return r;
  if (!r || typeof r !== "object") return [];
  for (const key of keys) if (Array.isArray(r[key])) return r[key];
  return [];
}

export { api };
