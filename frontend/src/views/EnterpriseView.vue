<script setup>
import { useApi, flatten, asList, api } from "../composables/useApi";
import PageHeader from "../components/PageHeader.vue";
import KvTable from "../components/KvTable.vue";
import DataTable from "../components/DataTable.vue";
import ApiAction from "../components/ApiAction.vue";

const { data: settings, run: r1 } = useApi(() => api.get("/api/v1/enterprise/settings").catch(() => null));
const { data: orgs, run: r2 } = useApi(() => api.get("/api/v1/enterprise/orgs").catch(() => null));
const { data: announcements, run: r3 } = useApi(() => api.get("/api/v1/enterprise/announcements").catch(() => null));
const refresh = () => { r1(); r2(); r3(); };
</script>

<template>
  <div class="max-w-5xl space-y-4">
    <PageHeader title="企业" sub="组织 / 设置 / 公告">
      <template #actions><button class="btn" @click="refresh">刷新</button></template>
    </PageHeader>
    <div class="grid lg:grid-cols-2 gap-4">
      <ApiAction title="创建组织" path="/api/v1/enterprise/orgs" :fields="[{ name: 'name', label: '组织名', placeholder: 'org' }, { name: 'type', label: '类型', default: 'enterprise' }]" />
      <ApiAction title="发布公告" path="/api/v1/enterprise/announcements" :fields="[{ name: 'title', label: '标题' }, { name: 'content', label: '内容' }]" />
    </div>
    <KvTable v-if="settings" :rows="flatten(settings)" />
    <DataTable v-if="orgs" :rows="asList(orgs, ['orgs', 'items'])" empty="暂无组织" />
    <DataTable v-if="announcements" :rows="asList(announcements, ['announcements', 'items'])" empty="暂无公告" />
  </div>
</template>
