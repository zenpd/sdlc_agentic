'use client';

import { useEffect, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { getGithubUser } from '@/lib/api';

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [authed, setAuthed] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    getGithubUser().then((user) => {
      if (cancelled) return;
      setAuthed(!!user);
      if (!user && pathname !== '/login') router.replace('/login');
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (pathname === '/login') return <>{children}</>;

  if (authed !== true) {
    return (
      <div style={{
        minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'var(--color-bg)', marginLeft: 'calc(-1 * var(--sidebar-width))',
      }}>
        <div style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>Loading…</div>
      </div>
    );
  }

  return <>{children}</>;
}
