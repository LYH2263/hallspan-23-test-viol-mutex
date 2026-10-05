<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const s = ref<any>({})
const noPlan = ref(false)
onMounted(async () => {
  try { s.value = await api('/seating/stats?hall_id=1') } catch { noPlan.value = true }
})
</script>
<template>
  <h1>统计</h1>
  <p class="sub">排座占用与违规汇总</p>
  <p v-if="noPlan" class="muted">暂无排座方案，请先在「排座图」执行排座</p>
  <div v-else class="card" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem">
    <div><div class="muted">已排座</div><div class="stat">{{ s.seated }}</div></div>
    <div><div class="muted">未排上</div><div class="stat">{{ s.unplaced }}</div></div>
    <div><div class="muted">违规数</div><div class="stat">{{ s.violations }}</div></div>
    <div><div class="muted">座位容量</div><div class="stat">{{ s.capacity }}</div></div>
  </div>
</template>
