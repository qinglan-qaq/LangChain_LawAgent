// Tailwind v4 下挂载 @inspira-ui/plugins 官方 JS 插件(bg-size / 背景纹理 / 颜色变量)
// 官方指南(Tailwind v3 用法)为 tailwind.config plugins 数组, v4 经 style.css 的
// @config 指令引入本文件等效加载。
import { setupInspiraUI } from "@inspira-ui/plugins";

export default {
  plugins: [setupInspiraUI],
};
