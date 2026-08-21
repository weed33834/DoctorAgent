<script setup>
import ApiAction from "../components/ApiAction.vue";
import PageHeader from "../components/PageHeader.vue";

// Sandboxed code execution — one reusable action panel.
</script>

<template>
  <div class="max-w-4xl space-y-4">
    <PageHeader title="沙箱执行" sub="受控代码运行" />
    <ApiAction
      title="运行代码"
      path="/api/v1/sandbox/run"
      help="在受控沙箱中执行 Python 代码（默认超时 / 受限网络）。"
      :fields="[
        { name: 'code', label: 'Python 代码', type: 'textarea', default: 'x = 1' },
        { name: 'timeout', label: '超时(秒)', type: 'number', default: 10 },
      ]"
    />
  </div>
</template>
