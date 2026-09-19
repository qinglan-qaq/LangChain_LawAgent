// 来源: Inspira UI 官方 registry (github.com/unovue/inspira-ui @ 04c57ab)
// app/components/inspira/ui/pattern-background/index.ts — TS 类型剥离, 逻辑原样
import { cva } from "class-variance-authority";

export const PATTERN_BACKGROUND_DIRECTION = {
  Top: "top",
  Bottom: "bottom",
  Left: "left",
  Right: "right",
  TopLeft: "top-left",
  TopRight: "top-right",
  BottomLeft: "bottom-left",
  BottomRight: "bottom-right",
};

export const PATTERN_BACKGROUND_VARIANT = {
  Grid: "grid",
  Dot: "dot",
  BigDot: "big-dot",
};

export const PATTERN_BACKGROUND_SPEED = {
  Default: 10000,
  Slow: 25000,
  Fast: 5000,
};

export const PATTERN_BACKGROUND_MASK = {
  Ellipse: "ellipse",
  EllipseTop: "ellipse-top",
};

export const patternBackgroundVariants = cva("relative text-clip", {
  variants: {
    variant: {
      [PATTERN_BACKGROUND_VARIANT.Grid]:
        "bg-[linear-gradient(to_right,#e4e4e7_1px,transparent_1px),linear-gradient(to_bottom,#e4e4e7_1px,transparent_1px)] dark:bg-[linear-gradient(to_right,#262626_1px,transparent_1px),linear-gradient(to_bottom,#262626_1px,transparent_1px)]",
      [PATTERN_BACKGROUND_VARIANT.Dot]:
        "bg-[radial-gradient(#d4d4d4_1px,transparent_1px)] dark:bg-[radial-gradient(#404040_1px,transparent_1px)]",
      [PATTERN_BACKGROUND_VARIANT.BigDot]:
        "bg-[radial-gradient(#d4d4d4_3px,transparent_3px)] dark:bg-[radial-gradient(#404040_3px,transparent_3px)]",
    },
    size: {
      xs: "bg-size-[8px_8px]",
      sm: "bg-size-[16px_16px]",
      md: "bg-size-[24px_24px]",
      lg: "bg-size-[32px_32px]",
      xl: "bg-size-[40px_40px]",
    },
  },
  defaultVariants: {
    variant: "grid",
    size: "md",
  },
});

export const patternBackgroundMaskVariants = cva("bg-default bg-background", {
  variants: {
    mask: {
      [PATTERN_BACKGROUND_MASK.Ellipse]:
        "mask-[radial-gradient(ellipse_at_center,transparent_20%,black)]",
      [PATTERN_BACKGROUND_MASK.EllipseTop]:
        "mask-[radial-gradient(ellipse_at_top,transparent_20%,black)]",
    },
  },
  defaultVariants: {
    mask: "ellipse",
  },
});

export { default as PatternBackground } from "./PatternBackground.vue";
