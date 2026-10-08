// 路由(D4): / 聊天页(原 App 内容) + /monitor 监控页
import { createRouter, createWebHistory } from 'vue-router'
import ChatPage from './views/ChatPage.vue'
import MonitorView from './views/MonitorView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'chat', component: ChatPage },
    { path: '/monitor', name: 'monitor', component: MonitorView },
  ],
})
