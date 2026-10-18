import { useEffect, useRef, useState } from 'react';
import type { BranchForm } from './branchForm';
import { BRANCH_STATUSES, COUNTRIES } from './branchForm';
const STATUS_META: Record<string, { color: string; bg: string; border: string }> = {
  Active:   { color: '#15803d', bg: '#f0fdf4', border: '#86efac' },
  Inactive: { color: '#92400e', bg: '#fffbeb', border: '#fcd34d' },
  Closed:   { color: '#6b7280', bg: '#f9fafb', border: '#d1d5db' },
};


function StatusPicker({ value, onChange, disabled }: {
  value: string; onChange: (v: string) => void; disabled: boolean; s: Record<string, string>;
}) {
  const [open, setOpen] = useState(false);
  const [dropStyle, setDropStyle] = useState<React.CSSProperties>({});
  const btnRef = useRef<HTMLButtonElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const meta = STATUS_META[value] ?? STATUS_META['Active'];

  const handleOpen = () => {
    if (disabled) return;
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setDropStyle({ position: 'fixed', top: r.bottom + 6, left: r.left, width: r.width, zIndex: 9999 });
    }
    setOpen(o => !o);
  };

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  return (
    <div ref={wrapRef}>
      <button
        ref={btnRef}
        type="button"
        disabled={disabled}
        onClick={handleOpen}
        style={{
          width: '100%', height: '40px', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '0 0.75rem', border: `1.5px solid ${open ? '#3b82f6' : '#e2e8f0'}`,
          borderRadius: '8px', background: '#fff', cursor: disabled ? 'not-allowed' : 'pointer',
          boxShadow: open ? '0 0 0 3px rgba(59,130,246,0.12)' : 'none',
          transition: 'border-color 0.14s, box-shadow 0.14s', fontFamily: 'inherit',
        }}
      >
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: '0.5rem',
          fontSize: '0.875rem', fontWeight: 600, color: meta.color,
          background: meta.bg, border: `1px solid ${meta.border}`,
          borderRadius: '20px', padding: '0.2rem 0.75rem',
        }}>
          <span style={{ width: 7, height: 7, borderRadius: '50%', background: meta.color, flexShrink: 0 }} />
          {value}
        </span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          style={{ flexShrink: 0, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.15s' }}>
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      {open && (
        <div style={{
          ...dropStyle,
          background: '#fff', border: '1px solid #e2e8f0', borderRadius: '10px',
          boxShadow: '0 8px 24px rgba(15,23,42,0.12)', overflow: 'hidden',
        }}>
          {BRANCH_STATUSES.map(st => {
            const m = STATUS_META[st];
            const isSelected = st === value;
            return (
              <button
                key={st} type="button"
                onClick={() => { onChange(st); setOpen(false); }}
                style={{
                  width: '100%', display: 'flex', alignItems: 'center', gap: '0.6rem',
                  padding: '0.65rem 0.9rem', background: isSelected ? '#f0f9ff' : 'transparent',
                  border: 'none', cursor: 'pointer', textAlign: 'left', fontFamily: 'inherit',
                  borderBottom: st !== 'Closed' ? '1px solid #f1f5f9' : 'none',
                }}
                onMouseEnter={e => { if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = '#f8fafc'; }}
                onMouseLeave={e => { if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = isSelected ? '#f0f9ff' : 'transparent'; }}
              >
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: m.color, flexShrink: 0 }} />
                <span style={{ fontSize: '0.875rem', fontWeight: isSelected ? 700 : 500, color: isSelected ? m.color : '#374151' }}>{st}</span>
                {isSelected && (
                  <svg style={{ marginLeft: 'auto', color: m.color }} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Custom country picker ─────────────────────────────────────────────────────

function CountryPicker({ value, onChange, disabled }: {
  value: string; onChange: (v: string) => void; disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [dropStyle, setDropStyle] = useState<React.CSSProperties>({});
  const btnRef = useRef<HTMLButtonElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  const handleOpen = () => {
    if (disabled) return;
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setDropStyle({ position: 'fixed', top: r.bottom + 6, left: r.left, width: r.width, zIndex: 9999 });
    }
    setOpen(o => !o);
  };

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const options = COUNTRIES.filter(c => c !== '');

  return (
    <div ref={wrapRef}>
      <button
        ref={btnRef} type="button" disabled={disabled} onClick={handleOpen}
        style={{
          width: '100%', height: '40px', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '0 0.75rem', border: `1.5px solid ${open ? '#3b82f6' : '#e2e8f0'}`,
          borderRadius: '8px', background: '#fff', cursor: disabled ? 'not-allowed' : 'pointer',
          boxShadow: open ? '0 0 0 3px rgba(59,130,246,0.12)' : 'none',
          transition: 'border-color 0.14s, box-shadow 0.14s', fontFamily: 'inherit',
          fontSize: '0.875rem', color: value ? '#0f172a' : '#94a3b8',
        }}
      >
        <span>{value || '— Select country —'}</span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          style={{ flexShrink: 0, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.15s' }}>
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      {open && (
        <div style={{
          ...dropStyle,
          background: '#fff', border: '1px solid #e2e8f0', borderRadius: '10px',
          boxShadow: '0 8px 24px rgba(15,23,42,0.12)', overflow: 'hidden',
          maxHeight: 220, overflowY: 'auto',
        }}>
          {value && (
            <button type="button"
              onClick={() => { onChange(''); setOpen(false); }}
              style={{ width: '100%', display: 'flex', alignItems: 'center', padding: '0.6rem 0.9rem', background: 'transparent', border: 'none', borderBottom: '1px solid #f1f5f9', cursor: 'pointer', fontFamily: 'inherit', fontSize: '0.8rem', color: '#94a3b8', textAlign: 'left' }}
            >Clear selection</button>
          )}
          {options.map(c => {
            const isSelected = c === value;
            return (
              <button key={c} type="button"
                onClick={() => { onChange(c); setOpen(false); }}
                style={{
                  width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  padding: '0.6rem 0.9rem', background: isSelected ? '#f0f9ff' : 'transparent',
                  border: 'none', borderBottom: '1px solid #f8fafc', cursor: 'pointer',
                  fontFamily: 'inherit', fontSize: '0.875rem',
                  fontWeight: isSelected ? 700 : 400, color: isSelected ? '#1d4ed8' : '#374151',
                  textAlign: 'left',
                }}
                onMouseEnter={e => { if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = '#f8fafc'; }}
                onMouseLeave={e => { if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = 'transparent'; }}
              >
                {c}
                {isSelected && <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#1d4ed8" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function BranchFormFields({
  form, onChange, disabled, isCreate, canSetDefault, timeZones, styles: s,
}: {
  form: BranchForm;
  onChange: (p: Partial<BranchForm>) => void;
  disabled: boolean;
  isCreate: boolean;
  canSetDefault: boolean;
  timeZones: string[];
  styles: Record<string, string>;
}) {
  return (
    <div className={s.formGrid}>
      {/* Branch Name — full width */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Branch Name <span className={s.required}>*</span></label>
        <input className={s.input} value={form.branch_name}
          onChange={(e) => onChange({ branch_name: e.target.value })}
          disabled={disabled} maxLength={120} autoFocus />
      </div>
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Branch timezone <span className={s.required}>*</span></label>
        <select className={s.input} value={form.time_zone_id}
          onChange={(e) => onChange({ time_zone_id: e.target.value })}
          disabled={disabled || timeZones.length === 0} required>
          <option value="">Select a timezone</option>
          {timeZones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
        </select>
        <p className={s.fieldHelp}>Used to determine this Branch’s business date and payroll WorkDates.</p>
      </div>
      {/* Status (half) | Country (half) */}
      <div className={s.formGroup}>
        <label className={s.label}>Status</label>
        <StatusPicker value={form.status} onChange={(v) => onChange({ status: v as BranchForm['status'] })} disabled={disabled} s={s} />
      </div>
      <div className={s.formGroup}>
        <label className={s.label}>Country</label>
        <CountryPicker value={form.country} onChange={(v) => onChange({ country: v })} disabled={disabled} />
      </div>
      {/* Address — full width */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Address</label>
        <input className={s.input} value={form.address_line1}
          onChange={(e) => onChange({ address_line1: e.target.value })}
          disabled={disabled} placeholder="Street address" maxLength={200} />
      </div>
      {/* City | State/Province */}
      <div className={s.formGroup}>
        <label className={s.label}>City</label>
        <input className={s.input} value={form.city}
          onChange={(e) => onChange({ city: e.target.value })} disabled={disabled} maxLength={100} />
      </div>
      <div className={s.formGroup}>
        <label className={s.label}>State / Province</label>
        <input className={s.input} value={form.state_province}
          onChange={(e) => onChange({ state_province: e.target.value })} disabled={disabled} maxLength={100} />
      </div>
      {/* Postal Code — half */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Postal Code</label>
        <input className={s.input} value={form.postal_code}
          onChange={(e) => onChange({ postal_code: e.target.value })} disabled={disabled} maxLength={20} />
      </div>
      {/* Notes — full width */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Notes</label>
        <textarea className={s.textarea} value={form.notes}
          onChange={(e) => onChange({ notes: e.target.value })}
          disabled={disabled} placeholder="Optional" maxLength={500} />
      </div>
      {isCreate && canSetDefault && (
        <div className={`${s.formGroup} ${s.formGroupFull}`}>
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem',
            cursor: 'pointer', fontSize: '0.875rem', fontWeight: 500, color: '#374151' }}>
            <input type="checkbox" checked={form.is_default}
              onChange={(e) => onChange({ is_default: e.target.checked })}
              disabled={disabled} style={{ accentColor: '#2563eb', width: 16, height: 16 }} />
            Set as default branch
          </label>
        </div>
      )}
      {isCreate && !canSetDefault && (
        <div className={`${s.formGroup} ${s.formGroupFull}`}>
          <p className={s.fieldHelp}>
            Only settings administrators can make a new Branch the Company default.
          </p>
        </div>
      )}
    </div>
  );
}

// ─── Small components ─────────────────────────────────────────────────────────
