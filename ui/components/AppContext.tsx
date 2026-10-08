'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { listApps, type Application } from '@/lib/api';

const STORAGE_KEY = 'activeAppId';
const DEFAULT_APP_ID = 'default';

type ActiveAppState = {
  apps: Application[];
  activeAppId: string;
  activeApp: Application | null;
  setActiveAppId: (id: string) => void;
  refreshApps: () => Promise<Application[]>;
};

const ActiveAppContext = createContext<ActiveAppState | null>(null);

// Which onboarded application this browser is working in. Kept per browser
// (localStorage) rather than on the server, so two people on two different
// applications never switch each other's context.
export function AppProvider({ children }: { children: React.ReactNode }) {
  const [apps, setApps] = useState<Application[]>([]);
  // Read synchronously on first render so pages never fire a request for the
  // default app first and then race it against the real one. (This provider
  // only mounts client-side behind AuthGate; the window check covers /login SSR.)
  const [activeAppId, setActiveAppIdState] = useState<string>(() => {
    if (typeof window === 'undefined') return DEFAULT_APP_ID;
    try { return localStorage.getItem(STORAGE_KEY) || DEFAULT_APP_ID; } catch { return DEFAULT_APP_ID; }
  });

  const setActiveAppId = useCallback((id: string) => {
    setActiveAppIdState(id);
    try { localStorage.setItem(STORAGE_KEY, id); } catch { /* ignore */ }
  }, []);

  const refreshApps = useCallback(async () => {
    try {
      const list = await listApps();
      setApps(list);
      return list;
    } catch {
      return [];
    }
  }, []);

  useEffect(() => { refreshApps(); }, [refreshApps]);

  // A stored id for an application that has since been deleted falls back to the default.
  useEffect(() => {
    if (apps.length && !apps.some((a) => a.id === activeAppId)) setActiveAppId(DEFAULT_APP_ID);
  }, [apps, activeAppId, setActiveAppId]);

  const value = useMemo<ActiveAppState>(() => ({
    apps,
    activeAppId,
    activeApp: apps.find((a) => a.id === activeAppId) ?? null,
    setActiveAppId,
    refreshApps,
  }), [apps, activeAppId, setActiveAppId, refreshApps]);

  return <ActiveAppContext.Provider value={value}>{children}</ActiveAppContext.Provider>;
}

export function useActiveApp(): ActiveAppState {
  const ctx = useContext(ActiveAppContext);
  if (!ctx) throw new Error('useActiveApp must be used inside <AppProvider>');
  return ctx;
}
