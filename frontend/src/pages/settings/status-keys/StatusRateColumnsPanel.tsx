import { useState } from 'react';
import type { StatusKey, StatusRateColumn } from '../../../types/settings';
import { activeStatusCount, canDeactivateColumn } from './statusColumnsModel';
import { AlertIcon, PlusIcon, SkeletonBlock, SpinnerIcon } from './StatusKeysDisplay';
import type { useStatusRateColumns } from './useStatusRateColumns';
import styles from './StatusKeysPage.module.css';

type Columns = ReturnType<typeof useStatusRateColumns>;

/**
 * Company Status rate columns: what makes a Status paid. Each column owns the one rate that a
 * driver's Status rate is set against (Pay Rates). The default column is the one new paid
 * Statuses start with; there is always exactly one.
 */
export function StatusRateColumnsPanel({
  columns, statusKeys, canEdit,
}: {
  columns: Columns;
  statusKeys: readonly StatusKey[];
  canEdit: boolean;
}) {
  const [newName, setNewName] = useState('');
  const [newDefault, setNewDefault] = useState(false);
  const [renaming, setRenaming] = useState<{ id: number; name: string } | null>(null);

  async function add() {
    if (await columns.create(newName, newDefault)) { setNewName(''); setNewDefault(false); }
  }

  async function saveRename(column: StatusRateColumn) {
    if (!renaming || renaming.id !== column.status_rate_column_id) return;
    if (await columns.update(column, { column_name: renaming.name.trim() }, 'Status rate column renamed.')) {
      setRenaming(null);
    }
  }

  return (
    <section className={styles.panel} aria-labelledby="status-columns-title">
      <div className={styles.panelHeader}>
        <div>
          <h2 id="status-columns-title" className={styles.sectionTitle}>Status rate columns</h2>
          <p className={styles.panelNote}>
            A Status bound to a column is paid its hours times the driver&apos;s rate for that column.
            Set each driver&apos;s rate under Pay Rates.
          </p>
        </div>
      </div>

      {columns.error && <div className={styles.errorAlert}><AlertIcon /> {columns.error}</div>}
      {columns.actionError && <div className={styles.errorAlert}><AlertIcon /> {columns.actionError}</div>}

      {columns.loading ? (
        <div className={styles.loadingCard}><SkeletonBlock /></div>
      ) : (
        <div className={styles.tableWrapFlat}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Column</th>
                <th>State</th>
                <th className={styles.numTh}>Active Statuses</th>
                {canEdit && <th />}
              </tr>
            </thead>
            <tbody>
              {columns.columns.map((column) => {
                const busy = columns.busyId === column.status_rate_column_id;
                const editing = renaming?.id === column.status_rate_column_id;
                return (
                  <tr key={column.status_rate_column_id} className={column.is_active ? undefined : styles.rowInactive}>
                    <td>
                      {editing ? (
                        <input
                          className={styles.input}
                          value={renaming.name}
                          onChange={(e) => setRenaming({ id: column.status_rate_column_id, name: e.target.value })}
                          aria-label="Column name"
                          autoFocus
                        />
                      ) : (
                        <span className={styles.statusCodeLabel}>{column.column_name}</span>
                      )}
                    </td>
                    <td>
                      {column.is_default && <span className={`${styles.badge} ${styles.badgeBlue}`}>Default</span>}{' '}
                      {column.is_active
                        ? <span className={`${styles.badge} ${styles.badgeGreen}`}>Active</span>
                        : <span className={`${styles.badge} ${styles.badgeRed}`}>Inactive</span>}
                    </td>
                    <td className={styles.numCell}>{activeStatusCount(column, statusKeys)}</td>
                    {canEdit && (
                      <td className={styles.rowActions}>
                        {busy ? <SpinnerIcon /> : editing ? (
                          <>
                            <button className={styles.btnPrimary} onClick={() => void saveRename(column)}>Save</button>
                            <button className={styles.btnSecondary} onClick={() => setRenaming(null)}>Cancel</button>
                          </>
                        ) : (
                          <>
                            <button
                              className={styles.btnGhost}
                              onClick={() => { columns.setActionError(''); setRenaming({ id: column.status_rate_column_id, name: column.column_name }); }}
                            >
                              Rename
                            </button>
                            {column.is_active && !column.is_default && (
                              <button
                                className={styles.btnGhost}
                                onClick={() => void columns.update(column, { is_default: true }, `"${column.column_name}" is now the default column.`)}
                              >
                                Make default
                              </button>
                            )}
                            {canDeactivateColumn(column, statusKeys) && (
                              <button
                                className={styles.btnGhost}
                                onClick={() => void columns.update(column, { is_active: false }, `"${column.column_name}" deactivated.`)}
                              >
                                Deactivate
                              </button>
                            )}
                            {!column.is_active && (
                              <button
                                className={styles.btnGhost}
                                onClick={() => void columns.update(column, { is_active: true }, `"${column.column_name}" reactivated.`)}
                              >
                                Reactivate
                              </button>
                            )}
                          </>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {canEdit && (
        <div className={styles.inlineForm}>
          <input
            className={styles.input}
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="New column name (e.g. Holiday pay)"
            aria-label="New Status rate column name"
            maxLength={200}
          />
          <label className={styles.checkboxLabel}>
            <input type="checkbox" checked={newDefault} onChange={(e) => setNewDefault(e.target.checked)} />
            <span>Make default</span>
          </label>
          <button className={styles.btnPrimary} onClick={() => void add()} disabled={columns.busyId === 'new'}>
            {columns.busyId === 'new' ? <SpinnerIcon /> : <PlusIcon />} Add column
          </button>
        </div>
      )}
    </section>
  );
}
