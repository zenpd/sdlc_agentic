'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import { useActiveApp } from '@/components/AppContext';
import {
  listSkills, getSkill, saveSkill, createSkill,
  listSkillVersions, getSkillVersion, restoreSkillVersion,
  getStageMap, listMarketplaceSkills, getMarketplaceSkill, installMarketplaceSkill,
  type SkillSummary, type SkillVersion, type MarketplaceSkill,
} from '@/lib/api';

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
  const { activeApp, activeAppId } = useActiveApp();
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [editContent, setEditContent] = useState('');
  const [editLoading, setEditLoading] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [versions, setVersions] = useState<SkillVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [previewVersionId, setPreviewVersionId] = useState<string | null>(null);
  const [previewContent, setPreviewContent] = useState('');
  const [restoreVersionId, setRestoreVersionId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newSlug, setNewSlug] = useState('app-skill');
  const [newContent, setNewContent] = useState('');
  const [createError, setCreateError] = useState<string | null>(null);
  const [createSaving, setCreateSaving] = useState(false);
  const [marketplaceOpen, setMarketplaceOpen] = useState(false);
  const [marketplaceLoading, setMarketplaceLoading] = useState(false);
  const [marketplaceError, setMarketplaceError] = useState<string | null>(null);
  const [marketplaceSkills, setMarketplaceSkills] = useState<MarketplaceSkill[]>([]);
  const [marketplaceQuery, setMarketplaceQuery] = useState('');
  const [marketplaceStages, setMarketplaceStages] = useState<string[]>([]);
  const [marketplaceStage, setMarketplaceStage] = useState('implementation');
  const [marketplaceSelected, setMarketplaceSelected] = useState<string | null>(null);
  const [marketplaceContent, setMarketplaceContent] = useState('');
  const [marketplacePreviewLoading, setMarketplacePreviewLoading] = useState(false);
  const [marketplaceInstalling, setMarketplaceInstalling] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      const result = await listSkills(undefined, activeAppId);
      if (seq === requestSeq.current) {
        setSkills(result);
        setError(null);
      }
    } catch (e: any) {
      if (seq === requestSeq.current) setError(e.message);
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [activeAppId]);

  useEffect(() => {
    setLoading(true);
    setEditingSlug(null);
    setEditContent('');
    setCreating(false);
    void refresh();
    return () => { requestSeq.current += 1; };
  }, [refresh]);

  const openEditor = async (slug: string) => {
    setCreating(false);
    setEditingSlug(slug);
    setEditLoading(true);
    setEditError(null);
    setSaved(false);
    setVersions([]);
    setVersionsError(null);
    setPreviewVersionId(null);
    try {
      const { content } = await getSkill(slug, activeAppId);
      setEditContent(content);
    } catch (e: any) {
      setEditError(e.message);
    } finally {
      setEditLoading(false);
    }
    setVersionsLoading(true);
    try {
      setVersions(await listSkillVersions(slug, activeAppId));
    } catch (e: any) {
      setVersionsError(e.message);
    } finally {
      setVersionsLoading(false);
    }
  };

  const closeEditor = () => {
    setEditingSlug(null);
    setEditContent('');
    setEditError(null);
    setSaved(false);
  };

  const { me, can } = useActiveApp();
  const canEdit = can('maintainer');

  const handleSave = async () => {
    if (!editingSlug || !canEdit) return;
    setSaving(true);
    setEditError(null);
    setSaved(false);
    try {
      await saveSkill(editingSlug, editContent, activeAppId);
      setSaved(true);
      setVersions(await listSkillVersions(editingSlug, activeAppId));
      await refresh();
    } catch (e: any) {
      setEditError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const handlePreviewVersion = async (versionId: string) => {
    if (!editingSlug) return;
    setVersionsError(null);
    try {
      const version = await getSkillVersion(editingSlug, versionId, activeAppId);
      setPreviewVersionId(versionId);
      setPreviewContent(version.content);
    } catch (e: any) {
      setVersionsError(e.message);
    }
  };

  const handleRestoreVersion = async (versionId: string) => {
    if (!editingSlug) return;
    if (!window.confirm(
      'Restore this version? Your current skill content will be saved in history first.',
    )) return;

    setRestoreVersionId(versionId);
    setVersionsError(null);
    setEditError(null);
    setSaved(false);
    try {
      const restored = await restoreSkillVersion(editingSlug, versionId, activeAppId);
      setEditContent(restored.content);
      setVersions(await listSkillVersions(editingSlug, activeAppId));
      setPreviewVersionId(null);
      setPreviewContent('');
      setSaved(true);
      await refresh();
    } catch (e: any) {
      setVersionsError(e.message);
    } finally {
      setRestoreVersionId(null);
    }
  };

  const startCreate = () => {
    setEditingSlug(null);
    setCreating(true);
    setNewSlug('app-skill');
    setNewContent(`---
name: app-skill
description: Describe when this app-specific persona should be used.
metadata:
  sdlc_stage: implementation
triggers:
  - add relevant keyword
---

Write the persona guidance for ${activeApp?.name || activeAppId} here.
`);
    setCreateError(null);
  };

  const handleCreate = async () => {
    setCreateSaving(true);
    setCreateError(null);
    const content = newContent.replace(
      /(^---\r?\nname:\s*).+$/m,
      (_match, namePrefix: string) => `${namePrefix}${newSlug}`,
    );
    try {
      await createSkill(newSlug, content, activeAppId);
      setCreating(false);
      await refresh();
    } catch (e: any) {
      setCreateError(e.message);
    } finally {
      setCreateSaving(false);
    }
  };

  const openMarketplace = async () => {
    setMarketplaceOpen(true);
    setMarketplaceError(null);
    setMarketplaceLoading(true);
    try {
      const [catalog, stageMap] = await Promise.all([
        listMarketplaceSkills(activeAppId),
        getStageMap(),
      ]);
      const stages = [...new Set(Object.values(stageMap).flat())];
      setMarketplaceSkills(catalog);
      setMarketplaceStages(stages);
      if (stages.length && !stages.includes(marketplaceStage)) {
        setMarketplaceStage(stages[0]);
      }
    } catch (e: any) {
      setMarketplaceError(e.message);
    } finally {
      setMarketplaceLoading(false);
    }
  };

  const previewMarketplaceSkill = async (slug: string) => {
    setMarketplaceSelected(slug);
    setMarketplacePreviewLoading(true);
    setMarketplaceError(null);
    try {
      const skill = await getMarketplaceSkill(slug);
      setMarketplaceContent(skill.content);
    } catch (e: any) {
      setMarketplaceError(e.message);
      setMarketplaceContent('');
    } finally {
      setMarketplacePreviewLoading(false);
    }
  };

  const handleMarketplaceInstall = async (slug: string) => {
    setMarketplaceInstalling(slug);
    setMarketplaceError(null);
    try {
      await installMarketplaceSkill(slug, marketplaceStage, activeAppId);
      setMarketplaceSkills((current) => current.map((skill) => (
        skill.slug === slug ? { ...skill, is_installed: true } : skill
      )));
      await refresh();
    } catch (e: any) {
      setMarketplaceError(e.message);
    } finally {
      setMarketplaceInstalling(null);
    }
  };

  const filteredMarketplaceSkills = marketplaceSkills.filter((skill) => (
    `${skill.name} ${skill.description}`.toLowerCase().includes(marketplaceQuery.toLowerCase())
  ));

  return (
    <div className="bg-bg min-h-screen">
      <Nav />

      <div className="content">
        {creating ? (
          <>
            <PageHeader
              eyebrow={`Skill for ${activeApp?.name || activeAppId}`}
              title="Create app-specific skill"
              description="This skill is stored for the selected application only and will not change the shared skill library."
              actions={
                <>
                  <button onClick={() => setCreating(false)} className="btn-secondary">← Back</button>
                  <button
                    onClick={handleCreate}
                    disabled={createSaving}
                    className="btn-primary"
                    style={{ opacity: createSaving ? 0.5 : 1 }}
                  >
                    {createSaving ? 'Creating…' : 'Create skill'}
                  </button>
                </>
              }
            />
            <div className="card-main" style={{ padding: 22 }}>
              {createError && <div className="alert error">{createError}</div>}
              <label className="label-main">Skill slug</label>
              <input
                value={newSlug}
                onChange={(e) => setNewSlug(e.target.value)}
                className="input-main"
                spellCheck={false}
                style={{ marginBottom: 16 }}
              />
              <label className="label-main">SKILL.md (frontmatter + body)</label>
              <textarea
                value={newContent}
                onChange={(e) => setNewContent(e.target.value)}
                className="input-main"
                spellCheck={false}
                style={{
                  minHeight: 520, fontFamily: 'var(--font-mono)', fontSize: 13,
                  lineHeight: 1.6, resize: 'vertical', whiteSpace: 'pre',
                }}
              />
            </div>
          </>
        ) : editingSlug ? (
          <>
            <PageHeader
              eyebrow="Persona skill"
              title={titleCase(editingSlug)}
              description={`Edit the SKILL.md used by ${activeApp?.name || activeAppId}. Saving creates or updates an app-only override; the shared skill stays unchanged.`}
              actions={
                <>
                  {saved && <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--color-green)' }}>✓ Saved</span>}
                  <button onClick={closeEditor} className="btn-secondary">← Back</button>
                  <button
                    onClick={handleSave}
                    disabled={saving || editLoading || !canEdit}
                    className="btn-primary"
                    style={{ opacity: saving || editLoading || !canEdit ? 0.5 : 1 }}
                  >
                    {saving ? 'Saving…' : 'Save changes'}
                  </button>
                </>
              }
            />

            {editLoading ? (
              <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
            ) : (
              <>
                <div className="card-main" style={{ padding: 22 }}>
                  {editError && <div className="alert error">{editError}</div>}
                  {me && !canEdit && (
                    <div className="locked-note" style={{ marginBottom: 14 }}>🔒 Read-only — editing skills needs <b>maintainer</b> or higher. Your role is <b>{me.role}</b>.</div>
                  )}
                  <label className="label-main">SKILL.md (frontmatter + body)</label>
                  <textarea
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    readOnly={!canEdit}
                    className="input-main"
                    spellCheck={false}
                    style={{
                      minHeight: 520, fontFamily: 'var(--font-mono)', fontSize: 13,
                      lineHeight: 1.6, resize: 'vertical', whiteSpace: 'pre',
                    }}
                  />
                </div>

                <section className="card-main" style={{ padding: 22, marginTop: 16 }}>
                  <h2 style={{ fontSize: 16, fontWeight: 750, marginBottom: 6 }}>
                    Version history
                  </h2>
                  <p style={{ color: 'var(--color-text-muted)', fontSize: 13, marginBottom: 14 }}>
                    Each saved revision is retained here. Restoring a revision first preserves the current content, so the restore can also be undone.
                  </p>
                  {versionsError && <div className="alert error">{versionsError}</div>}
                  {versionsLoading ? (
                    <p style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>Loading history…</p>
                  ) : versions.length === 0 ? (
                    <p style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>
                      No previous versions yet. A version is created when you save a changed skill.
                    </p>
                  ) : (
                    <div style={{ display: 'grid', gap: 10 }}>
                      {versions.map((version) => (
                        <div
                          key={version.version_id}
                          style={{
                            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                            gap: 12, flexWrap: 'wrap', padding: 12, borderRadius: 10,
                            background: 'var(--color-bg)',
                          }}
                        >
                          <div>
                            <div style={{ fontSize: 13, fontWeight: 650 }}>
                              {new Date(version.saved_at).toLocaleString()}
                            </div>
                            <div style={{ color: 'var(--color-text-muted)', fontSize: 11, marginTop: 3 }}>
                              {version.version_id} · {version.size_bytes.toLocaleString()} bytes
                            </div>
                          </div>
                          <div style={{ display: 'flex', gap: 8 }}>
                            <button
                              className="btn-secondary"
                              onClick={() => handlePreviewVersion(version.version_id)}
                            >
                              {previewVersionId === version.version_id ? 'Previewing' : 'Preview'}
                            </button>
                            <button
                              className="btn-primary"
                              onClick={() => handleRestoreVersion(version.version_id)}
                              disabled={!canEdit || restoreVersionId !== null}
                            >
                              {restoreVersionId === version.version_id ? 'Restoring…' : 'Restore'}
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                  {previewVersionId && (
                    <div style={{ marginTop: 18 }}>
                      <div style={{
                        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                        gap: 12, marginBottom: 8,
                      }}>
                        <label className="label-main" style={{ marginBottom: 0 }}>
                          Preview · {previewVersionId}
                        </label>
                        <button
                          className="btn-secondary"
                          onClick={() => setPreviewVersionId(null)}
                        >
                          Close preview
                        </button>
                      </div>
                      <textarea
                        value={previewContent}
                        readOnly
                        className="input-main"
                        spellCheck={false}
                        style={{
                          minHeight: 300, fontFamily: 'var(--font-mono)', fontSize: 12,
                          lineHeight: 1.6, resize: 'vertical', whiteSpace: 'pre',
                        }}
                      />
                    </div>
                  )}
                </section>
              </>
            )}
          </>
        ) : (
          <>
            <PageHeader
              eyebrow="Agent personas"
              title="Persona Skills"
              description={
                <>
                  Skills are tagged with the SDLC stage where they apply. The pipeline loads skills only when the
                  active pipeline stage allows that tag. The coding agent loads implementation personas; a separate,
                  read-only Verify agent loads verification personas and must approve the diff before a PR is created.
                  Within each stage, an eligible persona is selected automatically.
                </>
              }
              actions={
                <>
                  <button
                    onClick={marketplaceOpen ? () => setMarketplaceOpen(false) : openMarketplace}
                    className="btn-secondary"
                  >
                    {marketplaceOpen ? 'Close marketplace' : 'Browse public skills'}
                  </button>
                  {canEdit && (
                    <button onClick={startCreate} className="btn-primary">
                      + Add app skill
                    </button>
                  )}
                </>
              }
            />
            <p style={{ margin: '-14px 0 18px', color: 'var(--color-text-muted)', fontSize: 13 }}>
              Showing effective skills for {activeApp?.name || activeAppId}. App skills override shared skills with the same slug.
            </p>

            {marketplaceOpen && (
              <section className="card-main" style={{ padding: 22, marginBottom: 20 }}>
                <h2 style={{ fontSize: 17, fontWeight: 750, marginBottom: 6 }}>
                  Public skills
                </h2>
                <p style={{ color: 'var(--color-text-muted)', fontSize: 13, marginBottom: 14 }}>
                  Browse the SDK&apos;s public marketplace and import a skill into this application&apos;s persona library.
                  Review the instructions before installing; imported skills are external content.
                </p>
                {marketplaceError && <div className="alert error">{marketplaceError}</div>}
                <div style={{
                  display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center',
                  marginBottom: 14,
                }}>
                  <input
                    aria-label="Search public skills"
                    placeholder="Search public skills"
                    value={marketplaceQuery}
                    onChange={(event) => setMarketplaceQuery(event.target.value)}
                    className="input-main"
                    style={{ flex: '1 1 260px' }}
                  />
                  <label style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>
                    Apply to stage{' '}
                    <select
                      value={marketplaceStage}
                      onChange={(event) => setMarketplaceStage(event.target.value)}
                      disabled={!marketplaceStages.length}
                      className="input-main"
                      style={{ marginLeft: 6 }}
                    >
                      {marketplaceStages.map((stage) => (
                        <option key={stage} value={stage}>{STAGE_LABELS[stage] || stage}</option>
                      ))}
                    </select>
                  </label>
                </div>

                {marketplaceLoading ? (
                  <p style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>Loading public catalog…</p>
                ) : !filteredMarketplaceSkills.length ? (
                  <p style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>
                    {marketplaceSkills.length ? 'No skills match your search.' : 'No public skills are available.'}
                  </p>
                ) : (
                  <div style={{
                    display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))',
                    gap: 10, maxHeight: 360, overflowY: 'auto',
                  }}>
                    {filteredMarketplaceSkills.map((skill) => (
                      <div
                        key={skill.slug}
                        style={{
                          padding: 14, borderRadius: 10, background: 'var(--color-bg)',
                          border: marketplaceSelected === skill.slug
                            ? '1px solid var(--color-accent)' : '1px solid var(--color-border)',
                        }}
                      >
                        <div style={{ fontSize: 14, fontWeight: 700 }}>{titleCase(skill.name)}</div>
                        <p style={{
                          color: 'var(--color-text-secondary)', fontSize: 12, lineHeight: 1.5,
                          margin: '7px 0',
                        }}>
                          {skill.description}
                        </p>
                        <div style={{
                          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                          gap: 8, flexWrap: 'wrap',
                        }}>
                          <span style={{ color: 'var(--color-text-muted)', fontSize: 11 }}>
                            {skill.license ? `License: ${skill.license}` : 'License not specified'}
                          </span>
                          <div style={{ display: 'flex', gap: 7 }}>
                            <button
                              onClick={() => void previewMarketplaceSkill(skill.slug)}
                              className="btn-secondary"
                              style={{ padding: '6px 10px', fontSize: 12 }}
                            >
                              Preview
                            </button>
                            <button
                              onClick={() => void handleMarketplaceInstall(skill.slug)}
                              className="btn-primary"
                              disabled={
                                !canEdit
                                || skill.is_installed
                                || marketplaceInstalling !== null
                                || !marketplaceStages.length
                                || marketplaceSelected !== skill.slug
                                || !marketplaceContent
                              }
                              style={{ padding: '6px 10px', fontSize: 12 }}
                            >
                              {marketplaceInstalling === skill.slug
                                ? 'Installing…' : skill.is_installed ? 'Installed' : 'Install'}
                            </button>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {marketplaceSelected && (
                  <div style={{ marginTop: 18 }}>
                    <label className="label-main">
                      Preview · {marketplaceSelected}
                    </label>
                    {marketplacePreviewLoading ? (
                      <p style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>Loading preview…</p>
                    ) : (
                      <textarea
                        value={marketplaceContent}
                        readOnly
                        className="input-main"
                        spellCheck={false}
                        style={{
                          minHeight: 260, fontFamily: 'var(--font-mono)', fontSize: 12,
                          lineHeight: 1.6, resize: 'vertical', whiteSpace: 'pre',
                        }}
                      />
                    )}
                  </div>
                )}
              </section>
            )}

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
                      <span className="badge" style={{ fontSize: 11 }}>
                        {s.source === 'app'
                          ? s.overrides_shared ? 'App override' : 'App-only'
                          : 'Shared'}
                      </span>
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
                        {s.loads_in && (
                          <span
                            style={{
                              fontSize: 12,
                              color: s.loads_in.length
                                ? 'var(--color-text-muted)'
                                : 'var(--color-red)',
                            }}
                          >
                            {s.loads_in.length
                              ? `Available to pipeline stages: ${s.loads_in.join(', ')}`
                              : 'Not enabled for any pipeline stage'}
                          </span>
                        )}
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
