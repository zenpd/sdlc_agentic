'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { getGithubUser, hasRole, listApps, listTeams, updatePreferences, type Application, type GithubUser, type Role, type Team } from '@/lib/api';

const STORAGE_KEY = 'activeAppId';
const DEFAULT_APP_ID = 'default';

type ActiveAppState = {
  apps: Application[];        // every application this user may see
  teamApps: Application[];    // narrowed to the active team (+ shared ones)
  teams: Team[];              // the user's teams (admins: all teams)
  activeTeamId: string;       // '' = all of my teams
  setActiveTeamId: (id: string) => void;
  refreshTeams: () => Promise<void>;
  activeAppId: string;
  activeApp: Application | null;
  setActiveAppId: (id: string) => void;
  refreshApps: () => Promise<Application[]>;
  refreshMe: () => Promise<void>;
  // The signed-in human (not the agent persona) and what their role allows.
  me: GithubUser | null;
  can: (minimum: Role) => boolean;
};

const ActiveAppContext = createContext<ActiveAppState | null>(null);

// Which onboarded application this browser is working in. Kept per browser
// (localStorage) rather than on the server, so two people on two different
// applications never switch each other's context.
export function AppProvider({ children }: { children: React.ReactNode }) {
  const [apps, setApps] = useState<Application[]>([]);
  const [me, setMe] = useState<GithubUser | null>(null);
  const [teams, setTeams] = useState<Team[]>([]);
  const [activeTeamId, setActiveTeamIdState] = useState('');

  const refreshTeams = useCallback(async () => {
    try { setTeams(await listTeams()); } catch { /* not signed in yet */ }
  }, []);
  useEffect(() => { refreshTeams(); }, [refreshTeams]);

  const refreshMe = useCallback(async () => { setMe(await getGithubUser()); }, []);
  useEffect(() => { refreshMe(); }, [refreshMe]);
  // Read synchronously on first render so pages never fire a request for the
  // default app first and then race it against the real one. (This provider
  // only mounts client-side behind AuthGate; the window check covers /login SSR.)
  const storedAppId = useRef<string | null>(null);
  const [activeAppId, setActiveAppIdState] = useState<string>(() => {
    if (typeof window === 'undefined') return DEFAULT_APP_ID;
    try { storedAppId.current = localStorage.getItem(STORAGE_KEY); } catch { /* storage unavailable */ }
    return storedAppId.current || DEFAULT_APP_ID;
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

  // On a browser that has never picked an application, start on the user's
  // saved default (Account page) — that's what makes it follow them across devices.
  useEffect(() => {
    const preferred = me?.preferences?.default_app_id;
    if (!storedAppId.current && preferred && apps.some((a) => a.id === preferred)) {
      storedAppId.current = preferred;
      setActiveAppId(preferred);
    }
  }, [me, apps, setActiveAppId]);

  // The active team is a saved preference, so it follows the user to any device.
  useEffect(() => { setActiveTeamIdState(me?.preferences?.active_team_id ?? ''); }, [me]);

  const setActiveTeamId = useCallback((id: string) => {
    setActiveTeamIdState(id);
    updatePreferences({ active_team_id: id }).catch(() => {});
  }, []);

  const teamApps = useMemo(
    () => (activeTeamId ? apps.filter((a) => !a.team_id || a.team_id === activeTeamId) : apps),
    [apps, activeTeamId],
  );

  // Switching team moves you off an application that isn't in it.
  useEffect(() => {
    if (teamApps.length && !teamApps.some((a) => a.id === activeAppId)) setActiveAppId(teamApps[0].id);
  }, [teamApps, activeAppId, setActiveAppId]);

  const value = useMemo<ActiveAppState>(() => ({
    apps,
    teamApps,
    teams,
    activeTeamId,
    setActiveTeamId,
    refreshTeams,
    activeAppId,
    activeApp: apps.find((a) => a.id === activeAppId) ?? null,
    setActiveAppId,
    refreshApps,
    refreshMe,
    me,
    can: (minimum: Role) => hasRole(me?.role, minimum),
  }), [apps, teamApps, teams, activeTeamId, setActiveTeamId, refreshTeams, activeAppId, setActiveAppId, refreshApps, refreshMe, me]);

  return <ActiveAppContext.Provider value={value}>{children}</ActiveAppContext.Provider>;
}

export function useActiveApp(): ActiveAppState {
  const ctx = useContext(ActiveAppContext);
  if (!ctx) throw new Error('useActiveApp must be used inside <AppProvider>');
  return ctx;
}
