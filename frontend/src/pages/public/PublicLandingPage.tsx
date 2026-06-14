import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import styles from './PublicLandingPage.module.css';

const workflowSteps = [
  'Setup company & branches',
  'Add drivers',
  'Configure pay rates',
  'Enter daily payroll',
  'Review',
  'Finalize',
  'Ledger',
];

const trustItems = [
  {
    title: 'Branch governance',
    copy: 'Keep payroll work scoped by company, branch, and operator responsibility.',
  },
  {
    title: 'Effective-dated rates',
    copy: 'Control driver rate history before payroll reaches review and final close.',
  },
  {
    title: 'Approval controls',
    copy: 'Move payroll through entry, review, approval, and finalization checkpoints.',
  },
  {
    title: 'Read-only ledger',
    copy: 'Close payroll into finalized records designed for lookup and audit follow-up.',
  },
  {
    title: 'Source visibility',
    copy: 'Preserve the rate and source context needed to explain payroll later.',
  },
];

const plans = [
  {
    name: 'Starter',
    fit: 'Single-branch teams preparing to leave spreadsheet payroll controls behind.',
  },
  {
    name: 'Professional',
    fit: 'Multi-branch operators that need review flow, rate history, and final ledger control.',
  },
  {
    name: 'Enterprise',
    fit: 'Transportation groups with deeper governance, branch separation, and audit needs.',
  },
];

function PremiumWaves() {
  return (
    <div className={styles.premiumWaves} aria-hidden="true">
      <svg viewBox="0 0 1200 420" preserveAspectRatio="none">
        <defs>
          <linearGradient id="waveStroke" x1="0" x2="1">
            <stop offset="0%" stopColor="#0B1B3A" stopOpacity="0.07" />
            <stop offset="42%" stopColor="#2563EB" stopOpacity="0.3" />
            <stop offset="72%" stopColor="#0F766E" stopOpacity="0.22" />
            <stop offset="100%" stopColor="#0B1B3A" stopOpacity="0.05" />
          </linearGradient>
          <linearGradient id="ribbonStroke" x1="0" x2="1">
            <stop offset="0%" stopColor="#60A5FA" stopOpacity="0" />
            <stop offset="48%" stopColor="#38BDF8" stopOpacity="0.24" />
            <stop offset="100%" stopColor="#14B8A6" stopOpacity="0" />
          </linearGradient>
          <filter id="softGlow" x="-20%" y="-60%" width="140%" height="220%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        <g className={styles.waveRibbons} fill="none" stroke="url(#ribbonStroke)" strokeLinecap="round">
          <path d="M-80 142 C120 52 180 232 330 162 C490 92 540 232 720 157 C890 82 960 202 1280 107" />
          <path d="M-80 276 C120 186 180 366 330 296 C490 226 540 366 720 291 C890 216 960 336 1280 241" />
        </g>

        <g className={styles.waveLines} fill="none" stroke="url(#waveStroke)" filter="url(#softGlow)">
          <path d="M-80 170 C120 80 180 260 330 190 C490 120 540 260 720 185 C890 110 960 230 1280 135" />
          <path d="M-80 195 C120 105 180 285 330 215 C490 145 540 285 720 210 C890 135 960 255 1280 160" />
          <path d="M-80 220 C120 130 180 310 330 240 C490 170 540 310 720 235 C890 160 960 280 1280 185" />
          <path d="M-80 245 C120 155 180 335 330 265 C490 195 540 335 720 260 C890 185 960 305 1280 210" />
          <path d="M-80 270 C120 180 180 360 330 290 C490 220 540 360 720 285 C890 210 960 330 1280 235" />
        </g>

        <g className={styles.wordmarkSmoke} fill="none" stroke="url(#waveStroke)" filter="url(#softGlow)">
          <path d="M-90 214 C112 136 232 318 388 242 C492 191 562 191 656 226 C766 267 884 228 1048 166 C1124 137 1198 124 1290 128" />
          <path d="M-90 238 C116 161 236 338 390 266 C500 215 568 215 664 250 C772 290 890 252 1052 190 C1132 160 1204 148 1290 152" />
          <path d="M-90 262 C120 186 240 358 394 290 C506 240 574 240 670 274 C780 314 896 276 1058 214 C1138 184 1208 172 1290 176" />
          <path d="M-90 286 C124 211 246 378 400 314 C514 265 582 265 678 298 C788 338 904 300 1064 238 C1144 208 1212 196 1290 200" />
        </g>

        <g filter="url(#softGlow)">
          <circle cx="210" cy="150" r="4" fill="#3B82F6" opacity="0.45" />
          <circle cx="575" cy="235" r="5" fill="#2563EB" opacity="0.35" />
          <circle cx="880" cy="175" r="4" fill="#60A5FA" opacity="0.45" />
          <circle cx="960" cy="255" r="3" fill="#1D4ED8" opacity="0.35" />
          <circle cx="740" cy="122" r="3" fill="#14B8A6" opacity="0.38" />
          <circle cx="430" cy="286" r="3" fill="#38BDF8" opacity="0.32" />
        </g>
      </svg>
    </div>
  );
}

export function PublicLandingPage() {
  const pageRef = useRef<HTMLElement | null>(null);
  const [activeSection, setActiveSection] = useState('product');

  useEffect(() => {
    const root = pageRef.current;
    if (!root) return undefined;

    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    if (reduceMotion.matches) {
      root.style.setProperty('--hero-progress', '0');
      return undefined;
    }

    let frame = 0;
    const updateProgress = () => {
      frame = 0;
      const progress = Math.min(Math.max(window.scrollY / 420, 0), 1);
      root.style.setProperty('--hero-progress', progress.toFixed(3));
    };
    const onScroll = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(updateProgress);
    };

    updateProgress();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);

    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
    };
  }, []);

  useEffect(() => {
    const sectionIds = ['product', 'workflow', 'security', 'pricing'];
    let frame = 0;

    const updateActiveSection = () => {
      frame = 0;
      const probeY = 120;
      let current = 'product';

      sectionIds.forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.getBoundingClientRect().top <= probeY) {
          current = id;
        }
      });

      setActiveSection((prev) => (prev === current ? prev : current));
    };

    const onScroll = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(updateActiveSection);
    };

    updateActiveSection();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);

    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
    };
  }, []);

  useEffect(() => {
    const root = pageRef.current;
    if (!root) return undefined;

    const sections = Array.from(root.querySelectorAll<HTMLElement>('[data-reveal]'));
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.setAttribute('data-revealed', 'true');
          observer.unobserve(entry.target);
        });
      },
      { rootMargin: '0px 0px -12% 0px', threshold: 0.16 },
    );

    sections.forEach((section) => {
      if (section.getBoundingClientRect().top < window.innerHeight * 0.94) {
        section.setAttribute('data-revealed', 'true');
      } else {
        observer.observe(section);
      }
    });

    return () => observer.disconnect();
  }, []);

  return (
    <main className={styles.page} ref={pageRef}>
      <header className={styles.header}>
        <Link className={styles.brandLink} to="/" aria-label="Flussra home">
          <span className={styles.wordmarkCrop}>
            <img src="/brand/flussra-wordmark.png" alt="Flussra" />
          </span>
        </Link>

        <nav className={styles.nav} aria-label="Main navigation">
          <a href="#product" className={activeSection === 'product' ? styles.activeNav : undefined}>Product</a>
          <a href="#workflow" className={activeSection === 'workflow' ? styles.activeNav : undefined}>Workflow</a>
          <a href="#pricing" className={activeSection === 'pricing' ? styles.activeNav : undefined}>Pricing</a>
          <a href="#security" className={activeSection === 'security' ? styles.activeNav : undefined}>Security</a>
        </nav>

        <div className={styles.headerActions}>
          <Link className={styles.loginButton} to="/login">Login</Link>
          <button type="button" className={styles.disabledButton} disabled>
            Subscribe soon
          </button>
        </div>
      </header>

      <section className={styles.hero} id="product">
        <div className={styles.heroCard}>
          <PremiumWaves />
          <div className={styles.heroCardGrid} aria-hidden="true" />
          <div className={styles.heroCardContent}>
            <img src="/brand/flussra-mark.png" alt="" className={styles.heroMark} />
            <span className={styles.heroWordmarkCrop}>
              <img src="/brand/flussra-wordmark.png" alt="Flussra" />
            </span>
            <p>ORGANIZE &bull; CONTROL &bull; REVIEW &bull; PAY</p>
          </div>
        </div>

        <div className={styles.heroMessage}>
          <span className={styles.eyebrow}>FLUSSRA DRIVER PAYROLL</span>
          <h1>Auditable driver payroll for transportation companies</h1>
          <p>
            Control driver rates, payroll entry, review, finalization, and ledger
            across every branch.
          </p>
          <div className={styles.heroActions}>
            <Link className={styles.primaryCta} to="/login">Login</Link>
            <button type="button" className={styles.secondaryCta} disabled>
              Start setup - coming soon
            </button>
          </div>
        </div>
      </section>

      <section className={styles.problemSection} data-reveal>
        <span className={styles.sectionKicker}>The problem</span>
        <div className={styles.problemGrid}>
          <h2>Branch payroll should not depend on fragile spreadsheets.</h2>
          <p>
            Transportation payroll often crosses branch-specific rules, manual rate
            changes, daily entries, and review decisions. Flussra keeps those steps
            organized around the payroll record instead of scattered across files.
          </p>
        </div>
      </section>

      <section className={styles.workflowSection} id="workflow" data-reveal>
        <div className={styles.sectionHeader}>
          <span className={styles.sectionKicker}>Workflow</span>
          <h2>One controlled path from setup to ledger.</h2>
        </div>
        <div className={styles.workflowRail}>
          {workflowSteps.map((step, index) => (
            <article className={styles.workflowStep} key={step}>
              <span>{String(index + 1).padStart(2, '0')}</span>
              <strong>{step}</strong>
            </article>
          ))}
        </div>
      </section>

      <section className={styles.trustSection} id="security" data-reveal>
        <div className={styles.sectionHeader}>
          <span className={styles.sectionKicker}>Trust</span>
          <h2>Built for payroll work that needs to be explained later.</h2>
        </div>
        <div className={styles.trustGrid}>
          {trustItems.map((item) => (
            <article className={styles.trustCard} key={item.title}>
              <span className={styles.trustLine} aria-hidden="true" />
              <h3>{item.title}</h3>
              <p>{item.copy}</p>
            </article>
          ))}
        </div>
      </section>

      <section className={styles.pricingSection} id="pricing" data-reveal>
        <div className={styles.sectionHeader}>
          <span className={styles.sectionKicker}>Pricing</span>
          <h2>Plans will open after beta setup.</h2>
        </div>
        <div className={styles.planGrid}>
          {plans.map((plan) => (
            <article className={styles.planCard} key={plan.name}>
              <h3>{plan.name}</h3>
              <div className={styles.price}>$--</div>
              <p>{plan.fit}</p>
              <button type="button" disabled>Coming soon</button>
            </article>
          ))}
        </div>
      </section>

      <footer className={styles.footer}>
        <div className={styles.footerBrand}>
          <img src="/brand/flussra-mark.png" alt="" aria-hidden="true" />
          <span>Controlled driver payroll for transportation companies.</span>
        </div>
        <Link to="/login">Login</Link>
      </footer>
    </main>
  );
}
