import type { StatusKey, StatusRateColumn } from '../../../types/settings';
import { DeactivateIcon, ReactivateIcon, SpinnerIcon } from './StatusKeysDisplay';
import { statusColumnName } from './statusKeysModel';

export function StatusKeyDetail({
  statusKey: k,
  columns,
  canEdit,
  isDeactivating,
  onEdit,
  onDeactivate,
  onReactivate,
  styles: s,
}: {
  statusKey: StatusKey;
  columns: readonly StatusRateColumn[];
  canEdit: boolean;
  isDeactivating: boolean;
  onEdit: () => void;
  onDeactivate: () => void;
  onReactivate: () => void;
  styles: Record<string, string>;
}) {
  const columnName = statusColumnName(columns, k.status_rate_column_id);

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

        <div className={s.keyDetailSection}>
          <p className={s.keyDetailSectionTitle}>Status</p>
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Category</span>
            <span className={s.keyDetailValue}>{k.category}</span>
          </div>
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Hours</span>
            <span className={s.keyDetailValue}>{Number(k.hours_value)}h</span>
          </div>
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Counts as a full day off</span>
            <span className={s.keyDetailValue}>{k.is_off_reason ? 'Yes' : 'No'}</span>
          </div>
          {k.is_off_reason && (
            <p className={s.keyDetailHint}>
              A day with this Status cannot also have ordinary work quantities.
            </p>
          )}
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Code</span>
            <span className={s.keyDetailValue}>{k.status_code}</span>
          </div>
        </div>

        <div className={s.keyDetailSection}>
          <p className={s.keyDetailSectionTitle}>Pay</p>
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Status rate column</span>
            <span className={s.keyDetailValue}>{columnName ?? 'Not paid'}</span>
          </div>
          <p className={s.keyDetailHint}>
            {columnName
              ? `Paid hours x each driver's rate for "${columnName}".`
              : 'No pay: this Status is recorded but never paid.'}
          </p>
        </div>

        <div className={s.keyDetailSection}>
          <p className={s.keyDetailSectionTitle}>Aliases</p>
          {k.aliases.length === 0 ? (
            <p className={s.plannedNote}>No aliases. Aliases are shortcuts typed in payroll entry.</p>
          ) : (
            <div className={s.aliasChips}>
              {k.aliases.map((alias) => <span key={alias} className={s.aliasChip}>{alias}</span>)}
            </div>
          )}
        </div>

        {k.is_in_use && (
          <div className={s.keyDetailSection}>
            <p className={s.lockNote}>
              In use by payroll entries or frozen payroll. Its hours, full-day-off setting,
              Category and rate column can no longer change; add a new Status and
              deactivate this one to replace it. Name and aliases stay editable.
            </p>
          </div>
        )}

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
