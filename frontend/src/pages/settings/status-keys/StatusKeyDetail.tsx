import type { StatusKey } from '../../../types/settings';
import { DeactivateIcon, ReactivateIcon, SpinnerIcon } from './StatusKeysDisplay';

export function StatusKeyDetail({
  statusKey: k,
  canEdit,
  isDeactivating,
  onEdit,
  onDeactivate,
  onReactivate,
  styles: s,
}: {
  statusKey: StatusKey;
  canEdit: boolean;
  isDeactivating: boolean;
  onEdit: () => void;
  onDeactivate: () => void;
  onReactivate: () => void;
  styles: Record<string, string>;
}) {
  const usageLimitRows: { label: string; enabled: boolean; value: number | null }[] = [
    { label: 'Per period',         enabled: k.limit_uses_per_period_enabled,    value: k.limit_uses_per_period },
    { label: 'Per driver',         enabled: k.limit_uses_per_driver_enabled,    value: k.limit_uses_per_driver },
    { label: 'Across all drivers', enabled: k.limit_uses_across_drivers_enabled, value: k.limit_uses_across_drivers },
    { label: 'Per day',            enabled: k.limit_uses_per_day_enabled,       value: k.limit_uses_per_day },
  ];
  const anyLimitEnabled = usageLimitRows.some(r => r.enabled);

  return (
    <div className={s.keyDetailContent}>

      {/* Header */}
      <div className={s.keyDetailHeader}>
        <div className={s.keyDetailTitleBlock}>
          <span className={s.keyDetailCodeLabel}>{k.key_name}</span>
          <span className={k.is_active
            ? `${s.badge} ${s.badgeGreen}`
            : `${s.badge} ${s.badgeRed}`}>
            {k.is_active ? 'Active' : 'Inactive'}
          </span>
        </div>
        {canEdit && (
          <button className={s.btnGhost} onClick={onEdit}>
            <EditIconInline /> Edit
          </button>
        )}
      </div>

      {/* Body */}
      <div className={s.keyDetailBody}>

        {/* Key Details */}
        <div className={s.keyDetailSection}>
          <p className={s.keyDetailSectionTitle}>Key Details</p>
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Hours Value</span>
            <span className={s.keyDetailValue}>{k.hours_value}h</span>
          </div>
        </div>

        {/* Leave Settings */}
        <div className={s.keyDetailSection}>
          <p className={s.keyDetailSectionTitle}>Leave / Off Settings</p>
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Leave / off reason</span>
            <span className={s.keyDetailValue}>{k.is_off_reason ? 'Yes' : 'No'}</span>
          </div>
          {k.is_off_reason && (
            <p className={s.keyDetailHint}>
              Can be used for leave/time-off tracking during a payroll period.
            </p>
          )}
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Deducts allowance</span>
            <span className={s.keyDetailValue} style={{ color: 'var(--color-text-muted, #888)', fontStyle: 'italic' }}>Future feature</span>
          </div>
        </div>

        {/* Usage Limits */}
        <div className={s.keyDetailSection}>
          <div className={s.keyDetailSectionHeader}>
            <p className={s.keyDetailSectionTitle}>Usage Limits</p>
            {!anyLimitEnabled && <span className={s.plannedBadge}>Not configured</span>}
          </div>
          {!anyLimitEnabled ? (
            <p className={s.plannedNote}>
              No usage limits set. Edit this key to add limits.
              <br />
              <em>Note: limits are stored but not yet enforced at payroll entry time.</em>
            </p>
          ) : (
            <>
              {usageLimitRows.filter(r => r.enabled).map(row => (
                <div key={row.label} className={s.keyDetailRow}>
                  <span className={s.keyDetailLabel}>{row.label}</span>
                  <span className={s.keyDetailValue}>{row.value ?? '—'}</span>
                </div>
              ))}
              <p className={s.plannedNote} style={{ marginTop: '0.4rem' }}>
                <em>Usage limits are stored but not yet enforced at payroll entry time.</em>
              </p>
            </>
          )}
        </div>

      </div>

      {/* Actions */}
      {canEdit && (
        <div className={s.keyDetailActions}>
          {k.is_active ? (
            <button
              className={s.btnRowDeactivate}
              onClick={onDeactivate}
              disabled={isDeactivating}
              style={{ width: 'auto', height: 'auto', padding: '0.38rem 0.85rem', fontSize: '0.82rem', gap: '0.35rem' }}
            >
              {isDeactivating ? <SpinnerIcon /> : <DeactivateIcon />}
              Deactivate
            </button>
          ) : (
            <button
              className={s.btnRowReactivate}
              onClick={onReactivate}
              disabled={isDeactivating}
              style={{ width: 'auto', height: 'auto', padding: '0.38rem 0.85rem', fontSize: '0.82rem', gap: '0.35rem' }}
            >
              {isDeactivating ? <SpinnerIcon /> : <ReactivateIcon />}
              Reactivate
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// An inline version of EditIcon used inside StatusKeyDetail (avoids re-export issue)
function EditIconInline() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
    </svg>
  );
}
