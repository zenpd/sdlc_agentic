'use client';

import { useEffect, useState, useCallback } from 'react';
import Nav from '@/components/Nav';
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
      <div className="bg-animation">
        <div className="bg-grid" />
        <div className="bg-orb bg-orb-1" />
        <div className="bg-orb bg-orb-2" />
        <div className="bg-orb bg-orb-3" />
      </div>

      <Nav />

      <div className="content" style={{ maxWidth: 1200, margin: '0 auto', padding: '84px 32px 48px' }}>
        {editingSlug ? (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 24, flexWrap: 'wrap' }}>
              <button
                onClick={closeEditor}
                className="btn-secondary"
                style={{ padding: '8px 16px', fontSize: 13 }}
              >
                ← Back
              </button>
              <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: '-0.01em' }}>
                {titleCase(editingSlug)}
              </h1>
              <div style={{ flex: 1 }} />
              {saved && (
                <span style={{ fontSize: 13, color: 'var(--color-green)' }}>✓ Saved</span>
              )}
              <button
                onClick={handleSave}
                disabled={saving || editLoading}
                className="btn-primary"
                style={{ padding: '10px 20px', fontSize: 13, opacity: saving || editLoading ? 0.5 : 1 }}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            </div>

            {editLoading ? (
              <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
            ) : (
              <div className="card-main" style={{ padding: 24 }}>
                {editError && (
                  <div style={{
                    marginBottom: 16, padding: '12px 16px', borderRadius: 10,
                    background: 'rgba(220,38,38,.08)', color: 'var(--color-red)', fontSize: 13,
                  }}>
                    {editError}
                  </div>
                )}
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
            <div style={{ marginBottom: 32 }}>
              <h1 style={{ fontSize: 28, fontWeight: 800, letterSpacing: '-0.01em', marginBottom: 6 }}>
                Persona Skills
              </h1>
              <p style={{ fontSize: 14, color: 'var(--color-text-secondary)', maxWidth: 640 }}>
                One SKILL.md per SDLC role. The bot picks the best-matching persona for
                each ticket automatically (or a ticket can name one explicitly with a
                &ldquo;Persona: &lt;slug&gt;&rdquo; line) — edit any of these to change
                how that persona behaves.
              </p>
            </div>

            {loading ? (
              <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
            ) : error ? (
              <div style={{ color: 'var(--color-red)', fontSize: 14 }}>Error: {error}</div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 16 }}>
                {skills.map((s) => (
                  <button
                    key={s.slug}
                    onClick={() => openEditor(s.slug)}
                    className="card-main"
                    style={{
                      padding: 20, textAlign: 'left', cursor: 'pointer', border: s.error
                        ? '1px solid var(--color-red)' : undefined,
                      display: 'flex', flexDirection: 'column', gap: 10,
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span style={{ fontSize: 15, fontWeight: 700 }}>{titleCase(s.slug)}</span>
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
