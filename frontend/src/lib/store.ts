import { create } from 'zustand'
import { api, setToken, getToken } from './api'
import type { User } from './types'

type Theme = 'dark' | 'light'

interface AuthState {
  user: User | null
  loading: boolean
  login: (email: string, password: string) => Promise<User>
  logout: () => Promise<void>
  bootstrap: () => Promise<void>
  setUser: (user: User | null) => void
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  loading: true,
  setUser: (user) => set({ user }),
  login: async (email, password) => {
    const response = await api.login(email, password)
    setToken(response.access_token)
    set({ user: response.user, loading: false })
    return response.user
  },
  logout: async () => {
    try {
      await api.logout()
    } catch {
      /* the token may already be gone — clearing locally is enough */
    }
    setToken(null)
    set({ user: null })
  },
  bootstrap: async () => {
    if (!getToken()) {
      set({ user: null, loading: false })
      return
    }
    try {
      const user = await api.me()
      set({ user, loading: false })
    } catch {
      setToken(null)
      set({ user: null, loading: false })
    }
  },
}))

interface UiState {
  theme: Theme
  sidebarCollapsed: boolean
  toggleTheme: () => void
  setTheme: (theme: Theme) => void
  toggleSidebar: () => void
}

const THEME_KEY = 'prt.theme'

function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle('theme-dark', theme === 'dark')
  document.documentElement.classList.toggle('theme-light', theme === 'light')
  localStorage.setItem(THEME_KEY, theme)
}

const initialTheme: Theme = (localStorage.getItem(THEME_KEY) as Theme) || 'dark'
applyTheme(initialTheme)

export const useUi = create<UiState>((set, get) => ({
  theme: initialTheme,
  sidebarCollapsed: localStorage.getItem('prt.sidebar') === '1',
  toggleTheme: () => {
    const next: Theme = get().theme === 'dark' ? 'light' : 'dark'
    applyTheme(next)
    set({ theme: next })
  },
  setTheme: (theme) => {
    applyTheme(theme)
    set({ theme })
  },
  toggleSidebar: () => {
    const next = !get().sidebarCollapsed
    localStorage.setItem('prt.sidebar', next ? '1' : '0')
    set({ sidebarCollapsed: next })
  },
}))
