import Link from 'next/link';
import Nav from '@/components/Nav';

export default function HomePage() {
  return (
    <div className="bg-bg min-h-screen">
      <div className="bg-animation">
        <div className="bg-grid" />
        <div className="bg-orb bg-orb-1" />
        <div className="bg-orb bg-orb-2" />
        <div className="bg-orb bg-orb-3" />
      </div>

      <div className="content">
        <Nav />

        {/* Hero */}
        <section className="hero">
          <div className="hero-badge">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="20 6 9 17 4 12" />
            </svg>
            Production-ready · <span className="mono">v2.4.1</span>
          </div>
          <h1>
            Autonomous <span className="gradient">SDLC Agent</span>
          </h1>
          <div className="hero-sub">
            Ticket In <span className="arrow-em">→</span> Pull Request Out
          </div>
          <p className="hero-desc">
            An autonomous engineering agent that reads your ticket, writes production code,
            generates tests, validates CI locally, and opens a review-ready pull request — without human intervention.
          </p>
          <div className="hero-cta">
            <Link href="/run" className="btn-primary">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
              Launch Pipeline
            </Link>
            <Link href="/dashboard" className="btn-secondary">
              View Dashboard
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="5" y1="12" x2="19" y2="12" />
                <polyline points="12 5 19 12 12 19" />
              </svg>
            </Link>
          </div>
        </section>

        {/* 3-step visual explainer */}
        <section className="pipeline-section">
          <div className="pipeline-label">How It Works</div>
          <div className="pipeline">
            <div className="step-card">
              <div className="step-header">
                <div className="step-num">STEP 01</div>
                <div className="step-icon">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
                    <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
                    <line x1="12" y1="22.08" x2="12" y2="12" />
                  </svg>
                </div>
              </div>
              <div className="step-title">Ticket Ingested</div>
              <div className="step-desc">Agent reads the ticket, acceptance criteria, and linked design docs.</div>
              <div className="step-visual">
                <div className="ticket-id">PROJ-482</div>
                <div className="ticket-title">Add rate-limit middleware to /api/v2/checkout</div>
                <div className="ticket-meta">
                  <span className="ticket-tag tag-priority">HIGH</span>
                  <span className="ticket-tag tag-status">IN PROGRESS</span>
                </div>
              </div>
            </div>

            <div className="pipeline-arrow">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="5" y1="12" x2="19" y2="12" />
                <polyline points="12 5 19 12 12 19" />
              </svg>
            </div>

            <div className="step-card">
              <div className="step-header">
                <div className="step-num">STEP 02</div>
                <div className="step-icon">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="16 18 22 12 16 6" />
                    <polyline points="8 6 2 12 8 18" />
                  </svg>
                </div>
              </div>
              <div className="step-title">Agent Codes &amp; Tests</div>
              <div className="step-desc">Implements the change, writes unit + integration tests, runs lint and CI locally.</div>
              <div className="step-visual" style={{ padding: 8 }}>
                <div className="terminal">
                  <div className="terminal-line muted">$ agent run --ticket PROJ-482</div>
                  <div className="terminal-line purple">→ analyzing codebase · 312 files</div>
                  <div className="terminal-line">→ editing src/middleware/rateLimit.ts</div>
                  <div className="terminal-line green">✓ 14 tests passed (842ms)</div>
                  <div className="terminal-line green">
                    ✓ lint clean · build ok<span className="cursor" />
                  </div>
                </div>
              </div>
            </div>

            <div className="pipeline-arrow">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="5" y1="12" x2="19" y2="12" />
                <polyline points="12 5 19 12 12 19" />
              </svg>
            </div>

            <div className="step-card">
              <div className="step-header">
                <div className="step-num">STEP 03</div>
                <div className="step-icon">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="6" cy="6" r="3" />
                    <circle cx="6" cy="18" r="3" />
                    <line x1="6" y1="9" x2="6" y2="15" />
                    <path d="M18 9v6" />
                    <circle cx="18" cy="18" r="3" />
                    <path d="M18 9a3 3 0 0 0 0-6h-6" />
                  </svg>
                </div>
              </div>
              <div className="step-title">PR Opens</div>
              <div className="step-desc">A review-ready PR is opened with description, tests, and CI checks green.</div>
              <div className="step-visual">
                <div className="pr-header">
                  <div className="pr-status" />
                  <div className="pr-id">PR #1284 · open</div>
                </div>
                <div className="pr-title">feat: add rate-limit middleware to checkout</div>
                <div className="pr-files">
                  <div className="pr-file">
                    <span className="pr-file-name">src/middleware/rateLimit.ts</span>
                    <span>
                      <span className="pr-file-add">+82</span> <span className="pr-file-del">−3</span>
                    </span>
                  </div>
                  <div className="pr-file">
                    <span className="pr-file-name">__tests__/rateLimit.test.ts</span>
                    <span>
                      <span className="pr-file-add">+64</span> <span className="pr-file-del">−0</span>
                    </span>
                  </div>
                </div>
                <div className="pr-stats">
                  <span className="pr-stat-add">+146</span>
                  <span className="pr-stat-del">−3</span>
                  <span style={{ color: 'var(--color-text-muted)' }}>2 files · CI green</span>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* Features strip */}
        <section className="features">
          <div className="feature">
            <div className="feature-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
            </div>
            <h3>Secure by Design</h3>
            <p>Sandboxed execution. No secrets in model context. SOC 2 aligned.</p>
          </div>
          <div className="feature">
            <div className="feature-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
              </svg>
            </div>
            <h3>Test-First</h3>
            <p>Every PR ships with passing unit and integration tests, or it doesn&apos;t ship.</p>
          </div>
          <div className="feature">
            <div className="feature-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
            </div>
            <h3>~4 min Median</h3>
            <p>From ticket ingestion to opened PR, median cycle time under four minutes.</p>
          </div>
          <div className="feature">
            <div className="feature-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
              </svg>
            </div>
            <h3>Human in Loop</h3>
            <p>Code review still required. The agent prepares, your team approves.</p>
          </div>
        </section>

        {/* Footer */}
        <footer className="footer">
          <div>© 2026 SDLC Agent · Autonomous Engineering Pipeline</div>
          <div className="footer-links">
            <a href="#">Docs</a>
            <a href="#">Status</a>
            <a href="#">Security</a>
            <a href="#">Changelog</a>
          </div>
        </footer>
      </div>
    </div>
  );
}
