<script setup>
import ApiAction from "../components/ApiAction.vue";
import PageHeader from "../components/PageHeader.vue";
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="安全扫描" sub="威胁检测 / 红队" />
    <div class="grid lg:grid-cols-2 gap-4">
      <ApiAction title="执行安全扫描" path="/api/v1/security/scan" :fields="[{ name: 'target', label: '目标 / 路径' }, { name: 'depth', label: '深度', type: 'number', default: 2 }]" />
      <ApiAction title="运行红队演练" path="/api/v1/security/redteam/run" :fields="[{ name: 'scenario', label: '场景' }, { name: 'target', label: '目标' }]" />
    </div>
  </div>
</template>
