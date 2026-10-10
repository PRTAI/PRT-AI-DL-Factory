import * as React from 'react'

/**
 * 提交表单的草稿缓存：切到别的页面再回来，已填的内容还在。
 *
 * 每个字段单独存一份 localStorage（键 `prt-draft:<scope>:<field>`），
 * 提交成功或点「清空草稿」时用 clearDraft(scope) 一次性清掉。
 */
const PREFIX = 'prt-draft:'

function read<T>(storageKey: string, initial: T): T {
  try {
    const raw = localStorage.getItem(storageKey)
    if (raw == null) return initial
    const parsed = JSON.parse(raw) as T
    // 默认值后来加了新字段时，旧草稿里没有——对象类型用默认值兜底补齐
    if (initial && typeof initial === 'object' && !Array.isArray(initial) && parsed && typeof parsed === 'object') {
      return { ...initial, ...parsed }
    }
    return parsed
  } catch {
    return initial
  }
}

export function useDraftState<T>(scope: string, field: string, initial: T) {
  const storageKey = `${PREFIX}${scope}:${field}`
  const [value, setValue] = React.useState<T>(() => read(storageKey, initial))
  React.useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(value))
    } catch {
      /* 配额满了就不缓存，不影响填写 */
    }
  }, [storageKey, value])
  return [value, setValue] as const
}

export function clearDraft(scope: string) {
  const prefix = `${PREFIX}${scope}:`
  for (const key of Object.keys(localStorage)) {
    if (key.startsWith(prefix)) localStorage.removeItem(key)
  }
}
