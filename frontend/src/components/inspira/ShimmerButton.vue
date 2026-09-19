<!--
  来源: Inspira UI 官方 registry (github.com/unovue/inspira-ui @ 04c57ab)
  app/components/inspira/ui/shimmer-button/ShimmerButton.vue
  官方站点改版期间从源仓直取, TS→JS 移植, 模板与样式原样。
  唯一偏离: 追加 disabled:cursor-not-allowed disabled:opacity-50
  (项目发送按钮 busy 态需要, 官方原版无禁用样式)
-->
<script setup>
import { cn } from "@inspira-ui/plugins";

defineProps({
    shimmerColor: { type: String, default: "#ffffff" },
    shimmerSize: { type: String, default: "0.05em" },
    borderRadius: { type: String, default: "100px" },
    shimmerDuration: { type: String, default: "3s" },
    background: { type: String, default: "rgba(0, 0, 0, 1)" },
    class: { type: null, required: false, default: undefined },
  })
</script>

<template>
  <button
    :style="{
      '--spread': '90deg',
      '--shimmer-color': shimmerColor,
      '--radius': borderRadius,
      '--speed': shimmerDuration,
      '--cut': shimmerSize,
      '--bg': background,
    }"
    :class="
      cn(
        `group relative z-0 flex transform-gpu cursor-pointer items-center justify-center overflow-hidden [border-radius:var(--radius)] border border-white/10 px-6 py-3 whitespace-nowrap text-white transition-transform duration-300 ease-in-out [background:var(--bg)] active:translate-y-px dark:text-black disabled:cursor-not-allowed disabled:opacity-50`,
        $props.class,
      )
    "
  >
    <div class="@container-[size] absolute inset-0 -z-30 overflow-visible blur-[2px]">
      <div
        class="animate-shimmer-btn-shimmer-slide absolute inset-0 aspect-[1] h-[100cqh] rounded-none [mask:none]"
      >
        <div
          class="animate-shimmer-btn-spin-around absolute -inset-full w-auto [translate:0_0] rotate-0 [background:conic-gradient(from_calc(270deg-(var(--spread)*0.5)),transparent_0,var(--shimmer-color)_var(--spread),transparent_var(--spread))]"
        />
      </div>
    </div>
    <slot />

    <div
      class="insert-0 absolute size-full transform-gpu rounded-2xl px-4 py-1.5 text-sm font-medium shadow-[inset_0_-8px_10px_#ffffff1f] transition-all duration-300 ease-in-out group-hover:shadow-[inset_0_-6px_10px_#ffffff3f] group-active:shadow-[inset_0_-10px_10px_#ffffff3f]"
    />

    <div
      class="absolute inset-(--cut) -z-20 [border-radius:var(--radius)] [background:var(--bg)]"
    />
  </button>
</template>

<style scoped>
@keyframes shimmer-btn-shimmer-slide {
  to {
    transform: translate(calc(100cqw - 100%), 0);
  }
}

@keyframes shimmer-btn-spin-around {
  0% {
    transform: translateZ(0) rotate(0);
  }
  15%,
  35% {
    transform: translateZ(0) rotate(90deg);
  }
  65%,
  85% {
    transform: translateZ(0) rotate(270deg);
  }
  100% {
    transform: translateZ(0) rotate(360deg);
  }
}

.animate-shimmer-btn-shimmer-slide {
  animation: shimmer-btn-shimmer-slide var(--speed) ease-in-out infinite alternate;
}

.animate-shimmer-btn-spin-around {
  animation: shimmer-btn-spin-around calc(var(--speed) * 2) infinite linear;
}
</style>
