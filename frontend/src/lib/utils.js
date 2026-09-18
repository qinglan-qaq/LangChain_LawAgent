import { clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

// shadcn-vue 惯例的 cn: clsx 拼接 + tailwind-merge 去冲突
export function cn(...inputs) {
  return twMerge(clsx(inputs))
}
