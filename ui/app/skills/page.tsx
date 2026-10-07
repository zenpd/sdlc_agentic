'use client';

import { useEffect, useState, useCallback } from 'react';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import { listSkills, getSkill, saveSkill, type SkillSummary } from '@/lib/api';

const STAGE_LABELS: Record<string, string> = {
  discovery: 'Discovery',
  design: 'Design',
  implementation: 'Implementation',
  verification: 'Verification',
  release: 'Release',
  operate: 'Operate',
};

function titleCase(slug: string): string {
  return slug.split('-').map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
}

export default function SkillsPage() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [editContent, setEditContent] = useState('');
  const [editLoading, setEditLoading] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setSkills(await listSkills());
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, [refresh]);

  const openEditor = async (slug: string) => {
    setEditingSlug(slug);
    setEditLoading(true);
    setEditError(null);
    setSaved(false);
    try {
      const { content } = await getSkill(slug);
      setEditContent(content);
    } catch (e: any) {
      setEditError(e.message);
    } finally {
      setEditLoading(false);
    }
  };

  const closeEditor = () => {
    setEditingSlug(null);
    setEditContent('');
    setEditError(null);
    setSaved(false);
  };

  const handleSave = async () => {
    if (!editingSlug) return;
    setSaving(true);
    setEditError(null);
    setSaved(false);
    try {
      await saveSkill(editingSlug, editContent);
      setSaved(true);
      await refresh();
    } catch (e: any) {
      setEditError(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="bg-bg min-h-screen">
      <Nav />

      <div className="content">
        {editingSlug ? (
          <>
            <PageHeader
              eyebrow="Persona skill"
              title={titleCase(editingSlug)}
              description="Edit the SKILL.md that shapes how this persona approaches a ticket. Changes apply on the next run."
              actions={
                <>
                  {saved && <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--color-green)' }}>✓ Saved</span>}
                  <button onClick={closeEditor} className="btn-secondary">← Back</button>
                  <button
                    onClick={handleSave}
                    disabled={saving || editLoading}
                    className="btn-primary"
                    style={{ opacity: saving || editLoading ? 0.5 : 1 }}
                  >
                    {saving ? 'Saving…' : 'Save changes'}
                  </button>
                </>
              }
            />

            {editLoading ? (
              <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
            ) : (
              <div className="card-main" style={{ padding: 22 }}>
                {editError && <div className="alert error">{editError}</div>}
                <label className="label-main">SKILL.md (frontmatter + body)</label>
                <textarea
                  value={editContent}
                  onChange={(e) => setEditContent(e.target.value)}
                  className="input-main"
                  spellCheck={false}
                  style={{
                    minHeight: 520, fontFamily: 'var(--font-mono)', fontSize: 13,
                    lineHeight: 1.6, resize: 'vertical', whiteSpace: 'pre',
                  }}
                />
              </div>
            )}
          </>
        ) : (
          <>
            <PageHeader
              eyebrow="Agent personas"
              title="Persona Skills"
              description={
                <>
                  One agent, multiple skills. The agent picks the best-matching persona for each ticket automatically
                  (or a ticket can name one with a &ldquo;Persona: &lt;slug&gt;&rdquo; line) — edit any of these to
                  change how that persona behaves.
                </>
              }
            />

            {loading ? (
              <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
            ) : error ? (
              <div className="alert error">Error: {error}</div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14 }}>
                {skills.map((s) => (
                  <button
                    key={s.slug}
                    onClick={() => openEditor(s.slug)}
                    className="card-main feature-card"
                    style={{
                      minHeight: 0, padding: 20, textAlign: 'left', border: s.error
                        ? '1px solid var(--color-red)' : undefined,
                      display: 'flex', flexDirection: 'column', gap: 10,
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                      <span className="feature-icon" style={{ width: 34, height: 34 }}>
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                          <circle cx="12" cy="8" r="6" /><polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88" />
                        </svg>
                      </span>
                      <span style={{ fontSize: 15, fontWeight: 750 }}>{titleCase(s.slug)}</span>
                      {s.sdlc_stage && (
                        <span className="badge in-progress" style={{ fontSize: 11 }}>
                          {STAGE_LABELS[s.sdlc_stage] || s.sdlc_stage}
                        </span>
                      )}
                    </div>
                    {s.error ? (
                      <span style={{ fontSize: 13, color: 'var(--color-red)' }}>
                        Failed to parse: {s.error}
                      </span>
                    ) : (
                      <>
                        <p style={{ fontSize: 13, color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>
                          {s.description}
                        </p>
                        {s.keywords && s.keywords.length > 0 && (
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 2 }}>
                            {s.keywords.slice(0, 6).map((kw) => (
                              <span
                                key={kw}
                                style={{
                                  fontSize: 11, padding: '3px 8px', borderRadius: 100,
                                  background: 'var(--color-bg)', color: 'var(--color-text-muted)',
                                  fontFamily: 'var(--font-mono)',
                                }}
                              >
                                {kw}
                              </span>
                            ))}
                          </div>
                        )}
                      </>
                    )}
                  </button>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
