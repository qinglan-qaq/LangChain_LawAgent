<!--
  来源: Inspira UI 官方 registry (github.com/unovue/inspira-ui @ 04c57ab)
  app/components/inspira/ui/typewriter-text/TypewriterText.vue
  官方站点改版期间从源仓直取, TS→JS 移植, 逻辑/模板/样式原样
-->
<script setup>
import { cn } from "@inspira-ui/plugins";
import { Motion } from "motion-v";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

defineOptions({ inheritAttrs: false });

const props = defineProps({
    text: { type: [String, Array], required: true },
    as: { type: String, default: "div" },
    speed: { type: Number, default: 50 },
    initialDelay: { type: Number, default: 0 },
    waitTime: { type: Number, default: 2000 },
    deleteSpeed: { type: Number, default: 30 },
    loop: { type: Boolean, default: true },
    showCursor: { type: Boolean, default: true },
    hideCursorOnType: { type: Boolean, default: false },
    cursorChar: { type: String, default: "|" },
    cursorAnimationVariants: {
      type: Object,
      default: () => ({
        initial: { opacity: 0 },
        animate: {
          opacity: 1,
          transition: {
            duration: 0.01,
            repeat: Infinity,
            repeatDelay: 0.4,
            repeatType: "reverse",
          },
        },
      }),
    },
    cursorClass: { type: [String, Array], default: "ml-1" },
    class: { type: [String, Array], required: false, default: undefined },
  })

const texts = computed(() => (Array.isArray(props.text) ? props.text : [props.text]));
const displayText = ref("");
const currentIndex = ref(0);
const currentTextIndex = ref(0);
const isDeleting = ref(false);
let timeoutId;

const currentText = computed(() => texts.value[currentTextIndex.value] ?? "");
const hideCursor = computed(
  () =>
    props.hideCursorOnType && (currentIndex.value < currentText.value.length || isDeleting.value),
);

function clearTypingTimeout() {
  if (timeoutId) {
    clearTimeout(timeoutId);
    timeoutId = undefined;
  }
}

function scheduleTyping(delay) {
  clearTypingTimeout();
  timeoutId = setTimeout(typeNextCharacter, Math.max(0, delay));
}

function typeNextCharacter() {
  if (isDeleting.value) {
    if (!displayText.value) {
      isDeleting.value = false;

      if (currentTextIndex.value === texts.value.length - 1 && !props.loop) return;

      currentTextIndex.value = (currentTextIndex.value + 1) % texts.value.length;
      currentIndex.value = 0;
      scheduleTyping(props.waitTime);
      return;
    }

    displayText.value = displayText.value.slice(0, -1);
    currentIndex.value = displayText.value.length;
    scheduleTyping(props.deleteSpeed);
    return;
  }

  if (currentIndex.value < currentText.value.length) {
    displayText.value += currentText.value[currentIndex.value];
    currentIndex.value += 1;
    scheduleTyping(props.speed);
    return;
  }

  if (texts.value.length > 1) {
    isDeleting.value = true;
    scheduleTyping(props.waitTime);
  }
}

function resetTyping() {
  clearTypingTimeout();
  displayText.value = "";
  currentIndex.value = 0;
  currentTextIndex.value = 0;
  isDeleting.value = false;
  scheduleTyping(props.initialDelay);
}

onMounted(resetTyping);
onBeforeUnmount(clearTypingTimeout);

watch(() => props.text, resetTyping, { deep: true });
</script>

<template>
  <component
    :is="props.as"
    v-bind="$attrs"
    :class="cn('inline tracking-tight whitespace-pre-wrap', props.class)"
  >
    <span>{{ displayText }}</span>

    <!-- 官方源用 variant 标签式(initial/animate), motion-v 2.x 下标签不解析导致光标停在 opacity 0, 改对象式等价实现 -->
    <Motion
      v-if="props.showCursor"
      as="span"
      :initial="props.cursorAnimationVariants.initial"
      :animate="props.cursorAnimationVariants.animate"
      :class="cn(props.cursorClass, hideCursor && 'hidden')"
    >
      {{ props.cursorChar }}
    </Motion>
  </component>
</template>
