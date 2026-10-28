'use client';

import { useEffect, useState } from 'react';
import { completeManualStage, manualTaskNeedsHumanAction, type RunRecord } from '@/lib/api';
import ReviewCodeEditor from '@/components/ReviewCodeEditor';

type InlineReviewComment = {
  path: string;
  line: number;
  selectedText: string;
  text: string;
};

export default function ManualStagePanel({
  run,
  onCompleted,
}: {
  run: RunRecord;
  onCompleted: (updated: RunRecord) => void;
}) {
  const task = run.manual_task;
  const [values, setValues] = useState<Record<string, string>>({});
  const [files, setFiles] = useState<Record<string, string | null>>({});
  const [selectedFile, setSelectedFile] = useState('');
  const [selectedLine, setSelectedLine] = useState(1);
  const [selectedText, setSelectedText] = useState('');
  const [commentDraft, setCommentDraft] = useState('');
  const [inlineComments, setInlineComments] = useState<InlineReviewComment[]>([]);
  const [feedback, setFeedback] = useState('');
  const [verificationEvidence, setVerificationEvidence] = useState('');
  const [prFallback, setPrFallback] = useState(false);
  const [busy, setBusy] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValues(Object.fromEntries(
      (task?.fields ?? []).map((field) => [
        field.name,
        task?.initial_values[field.name] ?? '',
      ]),
    ));
    setFiles(Object.fromEntries(
      (task?.proposed_files ?? []).map((file) => [
        file.path,
        file.deleted ? null : file.content,
      ]),
    ));
    setSelectedFile(task?.proposed_files[0]?.path ?? '');
    setSelectedLine(1);
    setSelectedText('');
    setCommentDraft('');
    setInlineComments([]);
    setFeedback(task?.feedback ?? '');
    setVerificationEvidence('');
    setPrFallback(false);
    setError(null);
  }, [task?.stage_key, task?.attempts.length, task?.phase]);

  if (
    !task
    || run.status !== 'awaiting-human'
    || !manualTaskNeedsHumanAction(task)
  ) return null;
  const finalDiff = task.context.agent?.diff ?? '';
  const activeFile = task.proposed_files.find((file) => file.path === selectedFile)
    ?? task.proposed_files[0];
  const activeFileValue = activeFile
    ? files[activeFile.path] ?? activeFile.content
    : '';
  const hasUnappliedEdits = task.proposed_files.some((file) => {
    const currentContent = Object.prototype.hasOwnProperty.call(files, file.path)
      ? files[file.path]
      : file.deleted ? null : file.content;
    return currentContent !== (file.deleted ? null : file.content);
  });
  const evaluation = task.implementation_evaluation;
  const evaluatedCheckScore = evaluation?.checks.reduce(
    (total, check) => total + check.score,
    0,
  ) ?? null;
  const implementationScore = evaluation?.version === 6
    ? task.implementation_score
    : evaluatedCheckScore;
  const confidenceThreshold = evaluation?.threshold ?? 85;
  const expectedEvaluationChecks = [
    'requirements',
    'syntax',
    'static_analysis',
    'scope_safety',
    'robustness',
    'regression',
    'security',
  ];
  const scoredChecks = evaluation?.checks.filter(
    (check) => check.status !== 'not_run' && check.status !== 'error',
  ) ?? [];
  const scoredWeight = scoredChecks.reduce((total, check) => total + check.weight, 0);
  const calculatedScore = evaluation?.checks.reduce(
    (total, check) => total + check.score,
    0,
  ) ?? 0;
  const hasRequiredReviewEvidence = (
    [
      'requirements',
      'syntax',
      'static_analysis',
      'scope_safety',
      'robustness',
      'regression',
      'security',
    ].every((key) => (
      evaluation?.checks.some((check) => (
        check.key === key && (check.status === 'passed' || check.status === 'warning')
      ))
    ))
  );
  const hasCriticalEvaluationFailure = (
    (evaluation?.critical_failures.length ?? 0) > 0
    || (evaluation?.checks.some((check) => check.critical) ?? false)
  );
  const hasCompleteEvaluation = (
    evaluation?.version === 6
    && evaluation.checks.length === expectedEvaluationChecks.length
    && expectedEvaluationChecks.every((key) => evaluation.checks.some((check) => check.key === key))
    && evaluation.scored_weight === scoredWeight
    && calculatedScore === evaluation.score
    && evaluation.score === implementationScore
  );
  const highConfidence = (
    hasCompleteEvaluation
    && hasRequiredReviewEvidence
    && typeof implementationScore === 'number'
    && implementationScore >= confidenceThreshold
    && !hasCriticalEvaluationFailure
  );

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCompleted(await completeManualStage(run.id, { result: values }));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not submit the stage result.');
    } finally {
      setBusy(false);
    }
  };

  const submitImplementation = async (
    action: 'request_changes' | 'apply_edits' | 'accept_implementation' | 'skip_high_confidence_review',
  ) => {
    setBusy(true);
    setBusyAction(action);
    setError(null);
    try {
      onCompleted(await completeManualStage(run.id, {
        action,
        files: (
          action !== 'skip_high_confidence_review' && task.proposed_files.length > 0
        ) ? files : undefined,
        feedback: action === 'request_changes' ? [
          feedback.trim(),
          inlineComments.length > 0
            ? `Line-specific review comments:\n${inlineComments.map((comment) => (
              `- ${comment.path}:${comment.line}${comment.selectedText ? ` ("${comment.selectedText}")` : ''}: ${comment.text}`
            )).join('\n')}`
            : '',
        ].filter(Boolean).join('\n\n') : undefined,
      }));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not submit the implementation review.');
    } finally {
      setBusy(false);
      setBusyAction(null);
    }
  };

  const addInlineComment = () => {
    if (!activeFile || !commentDraft.trim()) return;
    setInlineComments((comments) => [
      ...comments,
      {
        path: activeFile.path,
        line: selectedLine,
        selectedText: selectedText.trim(),
        text: commentDraft.trim(),
      },
    ]);
    setCommentDraft('');
  };

  const submitVerification = async (
    action:
      | 'accept_verification'
      | 'request_verification_fixes'
      | 'flag_verification_concern'
      | 'submit_manual_verification'
      | 'retry_verification'
      | 'retry_pr_creation'
      | 'retry_ticket_update',
    evidence?: string,
  ) => {
    setBusy(true);
    setError(null);
    try {
      onCompleted(await completeManualStage(run.id, {
        action,
        feedback: evidence ?? (feedback.trim() || undefined),
      }));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not submit the verification decision.');
    } finally {
      setBusy(false);
    }
  };

  const downloadPatch = (patch: string) => {
    const blob = new Blob([patch], { type: 'text/x-diff;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${run.ticket_key}-implementation.patch`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <form onSubmit={submit} className="card-main" style={{ padding: 22, marginBottom: 20, borderColor: '#d97706' }}>
      <div className="eyebrow">
        <span aria-hidden="true" style={{ marginRight: 6, fontSize: 14 }}>
          👤
        </span>
        Human action needed · {task.label}
      </div>
      <h3 style={{ marginBottom: 8 }}>{task.label}</h3>
      {task.stage_key === 'gate' && (
        <p style={{ color: 'var(--color-text-secondary)', fontSize: 13, lineHeight: 1.5, marginBottom: 16 }}>
          {task.instructions}
        </p>
      )}
      {task.stage_key === 'agent' && (
        <div className="tip-box" style={{ marginBottom: 16 }}>
          {task.phase === 'generating' && <strong>The coding agent is loading the repository and preparing a proposal…</strong>}
          {task.phase === 'applying' && <strong>Applying your edits to the implementation workspace…</strong>}
          {task.phase === 'validating' && <strong>Validating the final edited code and running configured checks…</strong>}
          {task.phase === 'review' && task.error && (
            <div className="alert error">{task.error}</div>
          )}
          {task.phase === 'failed' && (
            <div className="alert error">
              {task.error || 'The coding agent could not prepare a proposal.'}
            </div>
          )}
          {task.phase === 'validation_failed' && (
            <div className="alert error">
              <strong>
                Validation did not pass. Fix the reported issue in the editor, then save your edits to run the checks again.
              </strong>
              {task.test_output && <pre style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{task.test_output}</pre>}
            </div>
          )}
        </div>
      )}
      {task.stage_key === 'gate' && (
        <div className="tip-box" style={{ marginBottom: 16 }}>
          {task.analysis && <p style={{ marginTop: 0 }}>{task.analysis}</p>}
          {task.questions.length > 0 && (
            <>
              <strong>Questions to clarify</strong>
              <ol style={{ margin: '8px 0 0', paddingLeft: 22 }}>
                {task.questions.map((question, index) => (
                  <li key={`${index}-${question}`} style={{ marginTop: 5 }}>{question}</li>
                ))}
              </ol>
            </>
          )}
        </div>
      )}
      {task.stage_key === 'verify' && (
        <div className="tip-box" style={{ marginBottom: 18 }}>
          {task.phase === 'verifying' && (
            <strong>Running independent verification against the approved ticket and implementation…</strong>
          )}
          {task.phase === 'failed' && (
            <div className="alert error">
              <strong>Verification could not complete.</strong>
              {task.error && <pre style={{ whiteSpace: 'pre-wrap', marginBottom: 8 }}>{task.error}</pre>}
              <button
                className="btn-secondary"
                type="button"
                disabled={busy}
                onClick={() => submitVerification('retry_verification')}
              >
                {busy ? 'Retrying…' : 'Retry verification'}
              </button>
              <label className="label-main" style={{ marginTop: 10 }}>
                Concern (optional; required to stop the workflow)
                <textarea
                  className="input-main"
                  rows={3}
                  maxLength={3000}
                  value={feedback}
                  onChange={(event) => setFeedback(event.target.value)}
                  placeholder="Explain why this verification issue needs human investigation."
                />
              </label>
              <button
                className="btn-secondary"
                type="button"
                disabled={busy || !feedback.trim()}
                onClick={() => submitVerification('flag_verification_concern')}
              >
                Flag concern and stop workflow
              </button>
            </div>
          )}
          {task.report && (
            <>
              <p>
                <b>Verification:</b> {task.report.decision.toUpperCase()}
                {task.report.decision_reason && ` · ${task.report.decision_reason}`}
              </p>
              <details>
                <summary>Review verification details</summary>
                <ul style={{ marginTop: 6, paddingLeft: 22 }}>
                  {task.report.checks.map((check, index) => (
                    <li key={`${check.name}-${index}`} style={{ marginTop: 6 }}>
                      <b>{check.name}: {check.status.replace('_', ' ')}</b>
                      {' — '}{check.summary}
                      {check.output && (
                        <details style={{ marginTop: 4 }}>
                          <summary>Check output</summary>
                          <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 260, overflow: 'auto' }}>
                            {check.output}
                          </pre>
                        </details>
                      )}
                    </li>
                  ))}
                </ul>
                <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 480, overflow: 'auto' }}>
                  {task.report.review_report}
                </pre>
                {finalDiff && (
                  <button
                    className="btn-secondary"
                    type="button"
                    onClick={() => downloadPatch(finalDiff)}
                  >
                    Download final patch
                  </button>
                )}
              </details>
              {task.phase === 'review' && (
                <>
                  <button
                    className="btn-secondary"
                    type="button"
                    disabled={busy}
                    onClick={() => submitVerification('retry_verification')}
                  >
                    {busy ? 'Running verification…' : 'Run verification again'}
                  </button>
                  {task.report.decision === 'fail' && (
                    <div className="alert error">
                      The automated report did not pass. Review the findings and check
                      outputs below. You can request fixes, flag a concern, or perform
                      and document an independent manual verification.
                    </div>
                  )}
                  <label className="label-main" style={{ marginTop: 14 }}>
                    Manual verification evidence
                    <textarea
                      className="input-main"
                      rows={4}
                      maxLength={3000}
                      value={verificationEvidence}
                      onChange={(event) => setVerificationEvidence(event.target.value)}
                      placeholder="Describe the checks you personally performed, the results, and how they cover the approved requirements."
                    />
                  </label>
                  <button
                    className="btn-secondary"
                    type="button"
                    disabled={busy || verificationEvidence.trim().length < 30}
                    onClick={() => submitVerification(
                      'submit_manual_verification',
                      verificationEvidence.trim(),
                    )}
                  >
                    {busy ? 'Submitting…' : 'Submit my verification and continue'}
                  </button>
                  <label className="label-main" style={{ marginTop: 14 }}>
                    Fix request or concern (optional for acceptance; required otherwise)
                    <textarea
                      className="input-main"
                      rows={3}
                      maxLength={3000}
                      value={feedback}
                      onChange={(event) => setFeedback(event.target.value)}
                      placeholder="Describe implementation fixes or a concern for the audit record."
                    />
                  </label>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
                    {task.report.decision === 'pass' && (
                      <button
                        className="btn-primary"
                        type="button"
                        disabled={busy}
                        onClick={() => submitVerification('accept_verification')}
                      >
                        {busy ? 'Submitting…' : 'Accept report and continue to PR'}
                      </button>
                    )}
                    <button
                      className="btn-secondary"
                      type="button"
                      disabled={busy || !feedback.trim()}
                      onClick={() => submitVerification('request_verification_fixes')}
                    >
                      Request implementation fixes
                    </button>
                    <button
                      className="btn-secondary"
                      type="button"
                      disabled={busy || !feedback.trim()}
                      onClick={() => submitVerification('flag_verification_concern')}
                    >
                      Flag concern and stop workflow
                    </button>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      )}
      {task.stage_key === 'pr' && (
        <div className="tip-box" style={{ marginBottom: 16 }}>
          {task.phase === 'creating' && (
            <strong>Creating pull request…</strong>
          )}
          {task.phase === 'awaiting_approval' && (
            <strong>Waiting for required approval…</strong>
          )}
          {task.phase === 'failed' && (
            <>
              <div className="alert error" style={{ marginBottom: 12 }}>
                <strong>Pull request creation failed.</strong>
                {task.error && <pre style={{ whiteSpace: 'pre-wrap', marginBottom: 8 }}>{task.error}</pre>}
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
                <button
                  className="btn-secondary"
                  type="button"
                  disabled={busy}
                  onClick={() => submitVerification('retry_pr_creation')}
                >
                  {busy ? 'Retrying…' : 'Retry'}
                </button>
                <button
                  className="btn-secondary"
                  type="button"
                  disabled={busy}
                  onClick={() => setPrFallback((current) => !current)}
                >
                  {prFallback ? 'Hide manual fallback' : 'Use manual fallback'}
                </button>
              </div>
            </>
          )}
          {prFallback && task.phase === 'failed' && (
            <div style={{ marginTop: 16 }}>
              {task.fields.map((field) => (
                <label key={field.name} className="label-main" style={{ marginBottom: 12 }}>
                  {field.label}{field.required ? ' *' : ''}
                  <input
                    className="input-main"
                    required={field.required}
                    maxLength={10000}
                    value={values[field.name] ?? ''}
                    onChange={(event) => setValues((current) => ({
                      ...current,
                      [field.name]: event.target.value,
                    }))}
                  />
                </label>
              ))}
              <button className="btn-primary" type="submit" disabled={busy}>
                {busy ? 'Verifying…' : 'Validate and continue'}
              </button>
            </div>
          )}
        </div>
      )}
      {task.stage_key === 'jira' && (
        <div className="tip-box" style={{ marginBottom: 18 }}>
          {task.phase === 'updating' && (
            <strong>Updating ticket…</strong>
          )}
          {task.phase === 'failed' && (
            <div className="alert error" style={{ marginBottom: 12 }}>
              <strong>The automatic ticket update could not be confirmed.</strong>
              {task.error && <pre style={{ whiteSpace: 'pre-wrap', marginBottom: 8 }}>{task.error}</pre>}
              <button
                className="btn-secondary"
                type="button"
                disabled={busy}
                onClick={() => submitVerification('retry_ticket_update')}
              >
                {busy ? 'Retrying…' : 'Retry ticket update'}
              </button>
            </div>
          )}
        </div>
      )}
      {task.stage_key === 'agent' && task.proposed_files.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginBottom: 18 }}>
          {task.phase === 'review' || task.phase === 'validation_failed' ? (
            <>
              <section className={`agent-confidence ${highConfidence ? 'high' : ''}`} aria-label="Implementation confidence score">
                <div className="agent-confidence-score">
                  <strong>{implementationScore === null ? '—' : `${implementationScore}%`}</strong>
                  <span>Confidence score</span>
                </div>
                <div className="agent-confidence-details">
                  <strong>
                    {task.phase === 'validation_failed'
                      ? 'Validation failed · review the code and fix the reported issues'
                      : hasCriticalEvaluationFailure
                        ? 'Critical quality check failed · human review required'
                        : !hasCompleteEvaluation || !hasRequiredReviewEvidence
                            ? 'Required quality evidence unavailable · human review required'
                      : highConfidence
                        ? 'High evaluation score · code review may be skipped'
                        : 'Evaluation score below threshold · code review is needed'}
                  </strong>
                </div>
                <div
                  className="agent-confidence-meter"
                  role="meter"
                  aria-label="Implementation confidence score"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={implementationScore ?? 0}
                >
                  <span style={{ width: `${Math.min(100, Math.max(0, implementationScore ?? 0))}%` }} />
                  <i style={{ left: `${confidenceThreshold}%` }} />
                </div>
              </section>
              {evaluation && (
                <details className="tip-box">
                  <summary>Evaluation evidence</summary>
                  {hasCriticalEvaluationFailure && (
                    <p className="alert error">
                      Code review cannot be skipped: {evaluation.critical_failures.join(', ')}.
                    </p>
                  )}
                  <ul style={{ marginTop: 8, paddingLeft: 22 }}>
                    {evaluation.checks.map((check) => (
                      <li key={check.key} style={{ marginTop: 8 }}>
                        <b>
                          {check.name}:{' '}
                          {check.status === 'not_run' || check.status === 'error'
                            ? `not scored (${check.score}/${check.weight})`
                            : `${check.score}/${check.weight}`}
                          {' · '}{check.status.replace('_', ' ')}
                        </b>
                        {' — '}{check.summary}
                        {check.output && (
                          <details style={{ marginTop: 4 }}>
                            <summary>Check details</summary>
                            <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 260, overflow: 'auto' }}>
                              {check.output}
                            </pre>
                          </details>
                        )}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              <section className="review-ide" aria-label="Implementation review editor">
                <div className="review-ide-toolbar">
                  <div className="review-file-tabs" aria-label="Changed files">
                    {task.proposed_files.map((file) => (
                      <button
                        key={file.path}
                        className={`review-file-tab ${file.path === (activeFile?.path ?? '') ? 'active' : ''}`}
                        type="button"
                        aria-pressed={file.path === (activeFile?.path ?? '')}
                        onClick={() => {
                          setSelectedFile(file.path);
                          setSelectedLine(1);
                          setSelectedText('');
                        }}
                      >
                        {file.deleted ? '−' : files[file.path] !== file.content ? '●' : '○'} {file.path}
                      </button>
                    ))}
                  </div>
                  <button
                    className="btn-primary review-apply-button"
                    type="button"
                    disabled={busy || !hasUnappliedEdits}
                    onClick={() => submitImplementation('apply_edits')}
                    title="Save your editor changes to the implementation workspace without starting validation."
                  >
                    {busyAction === 'apply_edits' ? 'Applying…' : 'Apply'}
                  </button>
                </div>
                {activeFile && (
                  <>
                    <div className="review-editor-heading">
                      <div>
                        <strong>{activeFile.path}</strong>
                        <span className="review-editor-status">
                          {activeFile.deleted
                            ? 'Deleted'
                            : files[activeFile.path] !== activeFile.content
                              ? 'Edited'
                              : 'Agent proposal'}
                        </span>
                      </div>
                    </div>
                    <ReviewCodeEditor
                      path={activeFile.path}
                      value={activeFileValue}
                      disabled={busy || files[activeFile.path] === null}
                      onChange={(content) => setFiles((current) => ({
                        ...current,
                        [activeFile.path]: content,
                      }))}
                      onSelectionChange={(line, text) => {
                        setSelectedLine(line);
                        setSelectedText(text);
                      }}
                    />
                    <div className="review-editor-footer">
                      <span>
                        Line {selectedLine}
                        {selectedText ? ` · ${selectedText.length} characters selected` : ''}
                      </span>
                      <label className="review-delete-toggle">
                        <input
                          type="checkbox"
                          checked={files[activeFile.path] === null}
                          disabled={busy}
                          onChange={(event) => setFiles((current) => ({
                            ...current,
                            [activeFile.path]: event.target.checked ? null : activeFile.content,
                          }))}
                        />
                        Delete file
                      </label>
                    </div>
                    <div className="review-comment-composer">
                      <div className="review-comment-heading">
                        <strong>Add line comment</strong>
                        <span>{activeFile.path}:{selectedLine}</span>
                      </div>
                      {selectedText && (
                        <code className="review-selected-text">
                          {selectedText.slice(0, 180)}
                        </code>
                      )}
                      <div className="review-comment-input-row">
                        <textarea
                          className="input-main"
                          rows={2}
                          maxLength={1000}
                          value={commentDraft}
                          onChange={(event) => setCommentDraft(event.target.value)}
                          placeholder="Select code or place the cursor on a line, then add a review comment."
                        />
                        <button
                          className="btn-secondary"
                          type="button"
                          disabled={!activeFile || files[activeFile.path] === null || !commentDraft.trim()}
                          onClick={addInlineComment}
                        >
                          Add comment
                        </button>
                      </div>
                      {inlineComments.length > 0 && (
                        <div className="review-comments-list">
                          {inlineComments.map((comment, index) => (
                            <article className="review-inline-comment" key={`${comment.path}-${comment.line}-${index}`}>
                              <div className="review-comment-heading">
                                <strong>{comment.path}:{comment.line}</strong>
                                <button
                                  className="review-remove-comment"
                                  type="button"
                                  onClick={() => setInlineComments((items) => items.filter((_, itemIndex) => itemIndex !== index))}
                                >
                                  Remove
                                </button>
                              </div>
                              {comment.selectedText && <code>{comment.selectedText.slice(0, 180)}</code>}
                              <p>{comment.text}</p>
                            </article>
                          ))}
                        </div>
                      )}
                    </div>
                  </>
                )}
              </section>
              <label className="label-main">
                Ask the coding agent for changes
                <textarea
                  className="input-main"
                  rows={3}
                  maxLength={3000}
                  value={feedback}
                  onChange={(event) => setFeedback(event.target.value)}
                  placeholder="Describe what the agent should change or fix."
                />
              </label>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
                <button
                  className="btn-secondary"
                  type="button"
                  disabled={busy || !feedback.trim()}
                  onClick={() => submitImplementation('request_changes')}
                >
                  {busy ? 'Sending…' : 'Request agent changes'}
                </button>
                <button
                  className="btn-primary"
                  type="button"
                  disabled={busy}
                  onClick={() => submitImplementation('accept_implementation')}
                >
                  {busy
                    ? 'Submitting…'
                    : task.phase === 'validation_failed'
                      ? 'Save edits and revalidate'
                      : 'Accept code and validate'}
                </button>
                {task.phase === 'review' && highConfidence && (
                  <button
                    className="btn-secondary"
                    type="button"
                    disabled={busy}
                    onClick={() => submitImplementation('skip_high_confidence_review')}
                  >
                    {busy ? 'Starting validation…' : 'Skip code review and validate'}
                  </button>
                )}
              </div>
            </>
          ) : null}
        </div>
      )}
      {task.stage_key === 'agent' && task.phase === 'failed' && task.proposed_files.length === 0 && (
        <div style={{ marginBottom: 18 }}>
          <label className="label-main">
            Retry instructions for the coding agent
            <textarea
              className="input-main"
              rows={3}
              maxLength={3000}
              value={feedback}
              onChange={(event) => setFeedback(event.target.value)}
              placeholder="For example, retry loading the repository and preparing the implementation."
            />
          </label>
          <button
            className="btn-secondary"
            type="button"
            disabled={busy || !feedback.trim()}
            onClick={() => submitImplementation('request_changes')}
          >
            {busy ? 'Retrying…' : 'Retry coding agent'}
          </button>
        </div>
      )}
      {task.stage_key !== 'agent' && task.stage_key !== 'pr' && task.stage_key !== 'jira' && task.fields.map((field) => (
        <label key={field.name} className="label-main" style={{ marginBottom: 12 }}>
          {field.label}{field.required ? ' *' : ''}
          {field.options ? (
            <select
              className="input-main"
              required={field.required}
              value={values[field.name] ?? ''}
              onChange={(e) => setValues((current) => ({ ...current, [field.name]: e.target.value }))}
            >
              <option value="">Select…</option>
              {field.options.map((option) => <option key={option} value={option}>{option.replace('_', ' ')}</option>)}
            </select>
          ) : field.multiline ? (
            <textarea
              className="input-main"
              required={field.required}
              maxLength={10000}
              rows={4}
              value={values[field.name] ?? ''}
              onChange={(e) => setValues((current) => ({ ...current, [field.name]: e.target.value }))}
            />
          ) : (
            <input
              className="input-main"
              required={field.required}
              maxLength={10000}
              value={values[field.name] ?? ''}
              onChange={(e) => setValues((current) => ({ ...current, [field.name]: e.target.value }))}
            />
          )}
        </label>
      ))}
      {error && <div className="alert error">{error}</div>}
      {task.stage_key !== 'agent'
        && task.stage_key !== 'verify'
        && task.stage_key !== 'pr'
        && task.stage_key !== 'jira'
        && <button className="btn-primary" type="submit" disabled={busy}>
        {busy
          ? task.stage_key === 'gate' ? 'Checking clarity…' : 'Submitting…'
          : task.stage_key === 'gate'
            ? 'Submit and check clarity'
            : 'Submit and continue'}
      </button>}
    </form>
  );
}
