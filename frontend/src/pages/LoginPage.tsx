import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import apiClient from '../lib/apiClient';
import { useAuth, toUserProfile } from '../store/authStore';
import type { UserInfoResponse } from '../store/authStore';
import styles from './LoginPage.module.css';

interface LoginResponse {
  access_token: string;
  token_type: string;
}

function getLoginErrorMessage(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })
    ?.response?.data?.detail;

  if (typeof detail === 'string' && detail.trim()) {
    return detail;
  }

  if (Array.isArray(detail) && detail.length > 0) {
    const msgs = detail
      .map((item: unknown) => {
        if (item && typeof item === 'object' && 'msg' in item) {
          return String((item as { msg: unknown }).msg);
        }
        return null;
      })
      .filter(Boolean);
    return msgs.length > 0
      ? msgs.join(' ')
      : 'Please fill in all required fields correctly.';
  }

  return 'Unable to sign in. Please check your details and try again.';
}

export function LoginPage() {
  const navigate = useNavigate();
  const { setUser, setLoading } = useAuth();

  const [fields, setFields] = useState({ company_code: '', username: '', password: '' });
  const [showPassword, setShowPassword] = useState(false);
  const [rememberMe, setRememberMe] = useState(false);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  function handleChange(e: React.ChangeEvent<HTMLInputElement>) {
    setFields((prev) => ({ ...prev, [e.target.name]: e.target.value }));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError('');
    setSubmitting(true);

    try {
      const { data } = await apiClient.post<LoginResponse>('/auth/login', {
        company_code: fields.company_code,
        username: fields.username,
        password: fields.password,
      });
      sessionStorage.setItem('access_token', data.access_token);

      setLoading(true);
      const me = await apiClient.get<UserInfoResponse>('/auth/me');
      setUser(toUserProfile(me.data));
      setLoading(false);

      navigate('/dashboard', { replace: true });
    } catch (err: unknown) {
      setLoading(false);
      sessionStorage.removeItem('access_token');
      setError(getLoginErrorMessage(err));
      setFields((prev) => ({ ...prev, password: '' }));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.page}>
      <section className={styles.formPanel} aria-label="Login form">
        <div className={styles.formScroll}>
          <div className={styles.formInner}>
            <div className={styles.formHeading}>
              <img src="/brand/flussra-mark.png" alt="" className={styles.formMark} />
              <h2 className={styles.welcomeTitle}>Welcome back</h2>
              <p className={styles.welcomeSub}>Sign in to manage driver payroll.</p>
            </div>

            {error && (
              <div className={styles.errorAlert} role="alert">
                <AlertIcon />
                <span>{error}</span>
              </div>
            )}

            <form onSubmit={handleSubmit} className={styles.form} noValidate>
              <div className={styles.fieldGroup}>
                <label className={styles.fieldLabel} htmlFor="company_code">Company Code</label>
                <div className={styles.inputWrap}>
                  <span className={styles.inputIcon}><BuildingIcon /></span>
                  <input
                    id="company_code"
                    name="company_code"
                    className={styles.input}
                    value={fields.company_code}
                    onChange={handleChange}
                    placeholder="e.g. DEMO"
                    autoComplete="organization"
                    spellCheck={false}
                    disabled={submitting}
                    required
                  />
                </div>
              </div>

              <div className={styles.fieldGroup}>
                <label className={styles.fieldLabel} htmlFor="username">Username</label>
                <div className={styles.inputWrap}>
                  <span className={styles.inputIcon}><UserIcon /></span>
                  <input
                    id="username"
                    name="username"
                    className={styles.input}
                    value={fields.username}
                    onChange={handleChange}
                    placeholder="Your username"
                    autoComplete="username"
                    spellCheck={false}
                    disabled={submitting}
                    required
                  />
                </div>
              </div>

              <div className={styles.fieldGroup}>
                <label className={styles.fieldLabel} htmlFor="password">Password</label>
                <div className={styles.inputWrap}>
                  <span className={styles.inputIcon}><LockIcon /></span>
                  <input
                    id="password"
                    name="password"
                    className={`${styles.input} ${styles.inputPassword}`}
                    type={showPassword ? 'text' : 'password'}
                    value={fields.password}
                    onChange={handleChange}
                    placeholder="Your password"
                    autoComplete="current-password"
                    disabled={submitting}
                    required
                  />
                  <button
                    type="button"
                    className={styles.eyeBtn}
                    onClick={() => setShowPassword((v) => !v)}
                    tabIndex={-1}
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                  >
                    {showPassword ? <EyeOffIcon /> : <EyeIcon />}
                  </button>
                </div>
              </div>

              <div className={styles.rememberRow}>
                <label className={styles.checkLabel}>
                  <input
                    type="checkbox"
                    className={styles.checkbox}
                    checked={rememberMe}
                    onChange={(e) => setRememberMe(e.target.checked)}
                  />
                  <span>Remember me</span>
                </label>
                <span className={styles.helpText}>
                  Need access? Contact your administrator.
                </span>
              </div>

              <button type="submit" className={styles.signInBtn} disabled={submitting}>
                {submitting ? (
                  <><SpinnerIcon /><span className={styles.signInLabel}>Signing in...</span></>
                ) : (
                  <>
                    <span className={styles.signInLabel}>Sign In</span>
                    <span className={styles.signInArrow}><ArrowRightIcon /></span>
                  </>
                )}
              </button>
            </form>

            <div className={styles.signupPrompt}>
              <span>New to Flussra?</span>
              <Link to="/signup">Sign up access coming soon</Link>
            </div>
          </div>
        </div>

        <div className={styles.formFooter}>
          <span>Copyright 2026 Flussra. All rights reserved.</span>
          <span>v1.0.0</span>
        </div>
      </section>

      <section className={styles.heroPanel} aria-label="Flussra payroll overview">
        <div className={styles.heroOverlay}>
          <div className={styles.heroContent}>
            <img src="/brand/flussra-wordmark.png" alt="Flussra" className={styles.heroWordmark} />
            <span className={styles.heroLabel}>Auditable Driver Payroll</span>
            <h1>Control driver payroll with confidence.</h1>
            <p>
              Rate history, branch scope, review controls, finalization, and ledger
              visibility for transportation payroll teams.
            </p>

            <ul className={styles.featureList}>
              <li>
                <BenefitShieldIcon />
                <span>Branch-aware access</span>
              </li>
              <li>
                <BenefitChartIcon />
                <span>Effective-dated rates</span>
              </li>
              <li>
                <BenefitCheckIcon />
                <span>Read-only ledger</span>
              </li>
            </ul>
          </div>
        </div>
      </section>
    </div>
  );
}

function BuildingIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="7" height="7" rx="1"/>
      <rect x="14" y="3" width="7" height="7" rx="1"/>
      <rect x="3" y="14" width="7" height="7" rx="1"/>
      <rect x="14" y="14" width="7" height="7" rx="1"/>
    </svg>
  );
}

function UserIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="8" r="4"/>
      <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>
    </svg>
  );
}

function LockIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="5" y="11" width="14" height="10" rx="2"/>
      <path d="M8 11V7a4 4 0 0 1 8 0v4"/>
    </svg>
  );
}

function EyeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
      <circle cx="12" cy="12" r="3"/>
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>
      <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>
      <line x1="1" y1="1" x2="23" y2="23"/>
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }} aria-hidden="true">
      <circle cx="12" cy="12" r="10"/>
      <line x1="12" y1="8" x2="12" y2="12"/>
      <line x1="12" y1="16" x2="12.01" y2="16"/>
    </svg>
  );
}

function ArrowRightIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="5" y1="12" x2="19" y2="12"/>
      <polyline points="12 5 19 12 12 19"/>
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" className={styles.spinner} aria-hidden="true">
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
    </svg>
  );
}

function BenefitShieldIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
      <polyline points="9 12 11 14 15 10"/>
    </svg>
  );
}

function BenefitChartIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="18" y1="20" x2="18" y2="10"/>
      <line x1="12" y1="20" x2="12" y2="4"/>
      <line x1="6" x2="6" y1="20" y2="14"/>
      <line x1="2" y1="20" x2="22" y2="20"/>
    </svg>
  );
}

function BenefitCheckIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  );
}
