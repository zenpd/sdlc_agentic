import { useMemo, type ReactElement } from 'react';

type ToolKind = 'terminal' | 'file_editor' | 'task_tracker' | 'other';

type ParsedLine = {
  tool: ToolKind | 'error';
  isResult: boolean;
  ok?: boolean;
  text: string;
};

const TOOL_META: Record<ToolKind, { label: string; color: string; icon: ReactElement }> = {
  terminal: {
    label: 'Terminal',
    color: '#34d399',
    icon: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="4 17 10 11 4 5" />
        <line x1="12" y1="19" x2="20" y2="19" />
      </svg>
    ),
  },
  file_editor: {
    label: 'File Editor',
    color: '#60a5fa',
    icon: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round">
        <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
        <path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4Z" />
      </svg>
    ),
  },
  task_tracker: {
    label: 'Task Tracker',
    color: '#fbbf24',
    icon: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="4" width="18" height="18" rx="2" />
        <polyline points="8 14 11 17 16 11" />
      </svg>
    ),
  },
  other: {
    label: 'Tool',
    color: '#c084fc',
    icon: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="8" />
      </svg>
    ),
  },
};

function parseLine(raw: string): ParsedLine | null {
  let m = raw.match(/^\[TOOL:(\w+)\]\s*(.*)$/);
  if (m) {
    const tool = (['terminal', 'file_editor', 'task_tracker'].includes(m[1]) ? m[1] : 'other') as ToolKind;
    return { tool, isResult: false, text: m[2] };
  }
  m = raw.match(/^\[RESULT:(\w+)\]\s*(ok|FAILED)$/);
  if (m) {
    const tool = (['terminal', 'file_editor', 'task_tracker'].includes(m[1]) ? m[1] : 'other') as ToolKind;
    return { tool, isResult: true, ok: m[2] === 'ok', text: m[2] };
  }
  m = raw.match(/^\[AGENT-ERROR\]\s*(.*)$/);
  if (m) return { tool: 'error', isResult: true, ok: false, text: m[1] };
  return null;
}

function basename(p: string): string {
  const parts = p.replace(/\\/g, '/').split('/');
  return parts[parts.length - 1] || p;
}

type NodeStatus = 'pending' | 'active' | 'done' | 'failed';

function Badge({ status, textMap }: { status: NodeStatus; textMap?: Partial<Record<NodeStatus, string>> }) {
  const cfg: Record<NodeStatus, { text: string; color: string; bg: string }> = {
    pending: { text: textMap?.pending ?? 'WAITING', color: '#5a5a78', bg: 'rgba(90,90,120,.15)' },
    active: { text: textMap?.active ?? 'RUNNING', color: '#fbbf24', bg: 'rgba(251,191,36,.14)' },
    done: { text: textMap?.done ?? 'DONE', color: '#34d399', bg: 'rgba(52,211,153,.14)' },
    failed: { text: textMap?.failed ?? 'FAILED', color: '#f87171', bg: 'rgba(248,113,113,.14)' },
  };
  const c = cfg[status];
  return (
    <span style={{
      fontSize: 9, fontWeight: 700, letterSpacing: '.06em', padding: '2px 7px', borderRadius: 100,
      color: c.color, background: c.bg, border: `1px solid ${c.color}33`,
      animation: status === 'active' ? 'traceBadgePulse 1.4s ease-in-out infinite' : 'none',
    }}>
      {c.text}
    </span>
  );
}

function TraceNode({
  title, status, children, flex,
}: { title: string; status: NodeStatus; children?: React.ReactNode; flex?: number }) {
  const glow = status === 'active' ? '#818cf8' : status === 'done' ? '#34d399' : status === 'failed' ? '#f87171' : '#22223a';
  return (
    <div style={{
      flex,
      width: flex ? undefined : '100%',
      border: `1px solid ${glow}`,
      borderRadius: 12,
      padding: '10px 14px',
      background: 'rgba(255,255,255,.02)',
      boxShadow: status === 'active' ? `0 0 0 1px ${glow}55, 0 0 18px ${glow}33` : 'none',
      transition: 'all .3s',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginBottom: children ? 6 : 0 }}>
        <span style={{ fontSize: 11.5, fontWeight: 700, color: '#e2e8f0' }}>{title}</span>
        <Badge status={status} />
      </div>
      {children}
    </div>
  );
}

function TraceConnector({ active, color = '#818cf8' }: { active: boolean; color?: string }) {
  return (
    <div style={{ position: 'relative', width: 2, height: 20, background: active ? `${color}40` : '#22223a', flexShrink: 0 }}>
      {active && (
        <div style={{
          position: 'absolute', left: -3, width: 8, height: 8, borderRadius: '50%',
          background: color, boxShadow: `0 0 10px 2px ${color}`,
          animation: 'traceFlow 1.1s linear infinite',
        }} />
      )}
    </div>
  );
}

function MiniStat({ label, value, color, mono, pulsing }: { label: string; value: string; color: string; mono?: boolean; pulsing?: boolean }) {
  return (
    <div style={{
      border: `1px solid ${pulsing ? color : '#22223a'}`,
      borderRadius: 9, padding: '7px 10px', minWidth: 0,
      background: pulsing ? `${color}14` : 'rgba(255,255,255,.02)',
      boxShadow: pulsing ? `0 0 12px ${color}33` : 'none',
      transition: 'all .25s',
    }}>
      <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: '.06em', color: '#5a5a78', marginBottom: 3 }}>
        {label}
      </div>
      <div style={{
        fontSize: mono ? 11 : 12, fontWeight: mono ? 400 : 700,
        fontFamily: mono ? 'var(--font-mono)' : 'inherit',
        color: pulsing ? color : '#c8c8dc',
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
      }}>
        {value}
      </div>
    </div>
  );
}

export default function AgentActivity({
  ticketKey, logs, stepStates, prUrl,
}: {
  ticketKey: string;
  logs: Record<string, string[]>;
  stepStates: string[];
  prUrl: string | null;
}) {
  const toNodeStatus = (s: string | undefined): NodeStatus => {
    if (s === 'in-progress') return 'active';
    if (s === 'success') return 'done';
    if (s === 'failed' || s === 'error' || s === 'human') return 'failed';
    return 'pending';
  };
  const [fetchState, , , gateState, agentState, prState] = stepStates.map(toNodeStatus);

  const fetchLine = logs.fetch?.[0] ?? '';
  const fetchMatch = fetchLine.match(/^(\S+):\s*(.*?)\s*\(persona:\s*([\w-]+)\)\s*$/);
  const summary = fetchMatch ? fetchMatch[2] : fetchLine;
  const persona = fetchMatch ? fetchMatch[3] : null;

  const gateLine = logs.gate?.[0] ?? '';

  const agentRaw = logs.agent ?? [];
  const parsed = useMemo(() => agentRaw.map(parseLine), [agentRaw]);
  const toolEvents = useMemo(() => parsed.filter((p): p is ParsedLine => p !== null), [parsed]);
  const plainLines = agentRaw.filter((_, i) => parsed[i] === null);

  const lastToolEvent = [...toolEvents].reverse().find((p) => p.tool !== 'error');
  const activeTool: ToolKind | null = agentState === 'active' && lastToolEvent && lastToolEvent.tool !== 'error'
    ? lastToolEvent.tool
    : null;
  const hasError = toolEvents.some((p) => p.tool === 'error');

  const lastAction = [...toolEvents].reverse().find((p) => !p.isResult && p.tool !== 'error');
  const targetLabel = lastAction?.tool === 'file_editor' ? 'File' : lastAction?.tool === 'terminal' ? 'Command' : 'Target';
  const targetValue = lastAction
    ? lastAction.tool === 'file_editor' ? basename(lastAction.text.split(' -> ').pop() || lastAction.text)
    : lastAction.tool === 'terminal' ? lastAction.text.replace(/^\$\s*/, '')
    : lastAction.text
    : '—';

  const lastResult = [...toolEvents].reverse().find((p) => p.isResult);
  const resultValue = lastResult ? (lastResult.ok ? 'ok' : 'FAILED') : '—';

  const modifiedLine = plainLines.find((l) => l.startsWith('Modified:'));
  const modifiedFiles = modifiedLine ? modifiedLine.replace('Modified:', '').split(',').map((s) => s.trim()).filter(Boolean) : [];
  const failLine = agentState === 'failed' ? plainLines[plainLines.length - 1] : null;

  const recent = [...toolEvents].slice(-5).reverse();
  const prNumber = prUrl ? prUrl.split('/').pop() : null;

  const running = agentState === 'active';
  const harnessGlow = hasError ? '#f87171' : running ? '#818cf8' : agentState === 'done' ? '#34d399' : '#22223a';

  if (!fetchLine && agentState === 'pending') return null;

  return (
    <div style={{
      background: '#0a0a16', border: '1px solid #1e1e2e', borderRadius: 16,
      padding: 22, marginBottom: 24, position: 'relative', overflow: 'hidden',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 18 }}>
        <span style={{
          width: 7, height: 7, borderRadius: '50%',
          background: running ? '#34d399' : '#5a5a78',
          boxShadow: running ? '0 0 8px #34d399' : 'none',
          animation: running ? 'traceBadgePulse 1.4s ease-in-out infinite' : 'none',
        }} />
        <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: '.08em', color: '#8a8aa8', textTransform: 'uppercase' }}>
          {running ? 'Live Agent Trace' : 'Agent Trace'}
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
        <TraceNode title={ticketKey || 'Ticket'} status={fetchLine ? 'done' : fetchState === 'active' ? 'active' : 'pending'}>
          {summary && (
            <div style={{ fontSize: 12, color: '#c8c8dc', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {summary}
            </div>
          )}
        </TraceNode>

        <TraceConnector active={!!fetchLine} color="#818cf8" />

        <TraceNode title="Clarity Gate" status={gateState}>
          {gateLine && (
            <div style={{ fontSize: 11, color: '#8a8aa8', fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {gateLine.replace(/^(YES|NO)\s*-\s*/, '')}
            </div>
          )}
        </TraceNode>

        <TraceConnector active={gateState === 'done'} color="#818cf8" />

        {/* ── Agent Harness ── */}
        <div style={{
          width: '100%', border: `1px solid ${harnessGlow}`, borderRadius: 14, padding: 16,
          background: running ? 'rgba(129,140,248,.05)' : 'rgba(255,255,255,.02)',
          boxShadow: running ? `0 0 0 1px ${harnessGlow}44, 0 0 24px ${harnessGlow}22` : 'none',
          transition: 'all .3s',
        }}>
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              <span style={{ fontSize: 12, fontWeight: 700, color: '#e2e8f0' }}>AGENT HARNESS</span>
              <span style={{ fontSize: 9.5, color: '#5a5a78', letterSpacing: '.05em' }}>REACT ORCHESTRATION LOOP</span>
            </div>
            <Badge status={agentState} />
          </div>

          {persona && (
            <div style={{ fontSize: 10.5, color: '#818cf8', fontFamily: 'var(--font-mono)', marginBottom: 10 }}>
              persona · {persona}
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(100px, 1fr))', gap: 8, marginBottom: 12 }}>
            {(['terminal', 'file_editor', 'task_tracker'] as ToolKind[]).map((t) => {
              const meta = TOOL_META[t];
              const isActive = activeTool === t;
              return (
                <div key={t} style={{
                  border: `1px solid ${isActive ? meta.color : '#22223a'}`, borderRadius: 9, padding: '7px 9px',
                  background: isActive ? `${meta.color}14` : 'rgba(255,255,255,.02)',
                  boxShadow: isActive ? `0 0 12px ${meta.color}33` : 'none',
                  display: 'flex', alignItems: 'center', gap: 7, transition: 'all .2s',
                }}>
                  <span style={{ color: isActive ? meta.color : '#5a5a78', display: 'flex' }}>{meta.icon}</span>
                  <span style={{ fontSize: 10.5, fontWeight: 600, color: isActive ? meta.color : '#8a8aa8' }}>{meta.label}</span>
                </div>
              );
            })}
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', gap: 8, marginBottom: 12 }}>
            <MiniStat label="ACTIVE TOOL" value={activeTool ? TOOL_META[activeTool].label : lastAction ? 'idle' : '—'} color={activeTool ? TOOL_META[activeTool].color : '#5a5a78'} pulsing={!!activeTool} />
            <MiniStat label={targetLabel.toUpperCase()} value={targetValue} color="#818cf8" mono pulsing={running && !!lastAction} />
            <MiniStat label="LAST RESULT" value={resultValue} color={resultValue === 'ok' ? '#34d399' : resultValue === 'FAILED' ? '#f87171' : '#5a5a78'} />
          </div>

          {recent.length > 0 && (
            <div style={{ borderTop: '1px solid #1e1e2e', paddingTop: 10, display: 'flex', flexDirection: 'column', gap: 3 }}>
              {recent.map((p, i) => (
                <div key={i} style={{
                  fontFamily: 'var(--font-mono)', fontSize: 10.5,
                  color: p.tool === 'error' ? '#f87171' : p.isResult ? (p.ok ? '#34d399' : '#f87171') : '#8a8aa8',
                  opacity: 1 - i * 0.15,
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>
                  {p.isResult ? (p.ok ? '✓ ' : '✗ ') : '▸ '}
                  {p.tool === 'error' ? p.text : p.isResult ? `${TOOL_META[p.tool as ToolKind]?.label ?? p.tool} ${p.ok ? 'succeeded' : 'failed'}` : p.text}
                </div>
              ))}
            </div>
          )}

          {failLine && (
            <div style={{ marginTop: 10, fontSize: 11, color: '#f87171', fontFamily: 'var(--font-mono)' }}>
              {failLine}
            </div>
          )}
        </div>

        <TraceConnector active={agentState === 'done'} color="#34d399" />

        <div style={{ display: 'flex', gap: 10, width: '100%' }}>
          <TraceNode title="Verify" status={modifiedFiles.length > 0 ? 'done' : agentState === 'failed' ? 'failed' : 'pending'} flex={1}>
            <div style={{ fontSize: 11, color: '#8a8aa8', fontFamily: 'var(--font-mono)' }}>
              {modifiedFiles.length > 0 ? `${modifiedFiles.length} file${modifiedFiles.length > 1 ? 's' : ''} changed` : '—'}
            </div>
          </TraceNode>
          <TraceNode title="Pull Request" status={prState} flex={1}>
            {prUrl ? (
              <a href={prUrl} target="_blank" rel="noopener noreferrer" style={{ fontSize: 11, color: '#34d399', fontFamily: 'var(--font-mono)', textDecoration: 'none' }}>
                #{prNumber} →
              </a>
            ) : (
              <div style={{ fontSize: 11, color: '#5a5a78', fontFamily: 'var(--font-mono)' }}>—</div>
            )}
          </TraceNode>
        </div>
      </div>

      <style>{`
        @keyframes traceFlow {
          0% { top: -4px; opacity: 0; }
          20% { opacity: 1; }
          100% { top: 20px; opacity: 0; }
        }
        @keyframes traceBadgePulse {
          0%, 100% { opacity: 1; }
          50% { opacity: .5; }
        }
      `}</style>
    </div>
  );
}
