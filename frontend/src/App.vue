<script setup>
import { ref } from 'vue'

const backend = ref(null)
const error = ref('')
const loading = ref(false)

// 走 vite proxy 的 /api 前缀,命中后端 GET /home
async function ping() {
  loading.value = true
  error.value = ''
  backend.value = null
  try {
    const res = await fetch('/api/home')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    backend.value = await res.json()
  } catch (e) {
    error.value = `后端未连通: ${e.message}`
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main>
    <h1>法律咨询 Agent</h1>
    <p>
      Vue 3 + Vite 前端骨架。后端 Legal Consultation API 默认监听
      <code>http://127.0.0.1:8000</code>。
    </p>
    <button :disabled="loading" @click="ping">
      {{ loading ? '请求中…' : '检测后端' }}
    </button>
    <p v-if="error" class="err">{{ error }}</p>
    <pre v-if="backend">{{ JSON.stringify(backend, null, 2) }}</pre>
  </main>
</template>

<style scoped>
main {
  max-width: 720px;
  margin: 4rem auto;
  padding: 0 1rem;
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
}
button {
  padding: 0.5rem 1rem;
  cursor: pointer;
}
.err {
  color: #c00;
}
pre {
  background: #f5f5f5;
padding: 1rem;
  overflow: auto;
}
</style>
