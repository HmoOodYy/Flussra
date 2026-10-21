import type { Dispatch, SetStateAction } from 'react';
import type { StatusKey, StatusRateColumn } from '../../../types/settings';
import { statusColumnName, type KeyFilter, type KeysLoadAction } from './statusKeysModel';
import { StatusKeyDetail } from './StatusKeyDetail';
import { AlertIcon, KeyIcon, PlusIcon, SkeletonBlock } from './StatusKeysDisplay';
import styles from './StatusKeysPage.module.css';

export function StatusKeysWorkspace({
  statusKeys, visibleKeys, columns, keysLoading, keysError, keyStatusFilter, setKeyStatusFilter, canEdit, openCreateKey,
  selectedKeyId, dispatchKeys, selectedKey, deactivating, openEditKey, requestDeactivate, requestReactivate,
}: {
  statusKeys: StatusKey[];
  visibleKeys: StatusKey[];
  columns: readonly StatusRateColumn[];
  keysLoading: boolean;
  keysError: string;
  keyStatusFilter: KeyFilter;
  setKeyStatusFilter: Dispatch<SetStateAction<KeyFilter>>;
  canEdit: boolean;
  openCreateKey: () => void;
  selectedKeyId: number | null;
  dispatchKeys: Dispatch<KeysLoadAction>;
  selectedKey: StatusKey | null;
  deactivating: number | null;
  openEditKey: (key: StatusKey) => void;
  requestDeactivate: (key: StatusKey) => void;
  requestReactivate: (key: StatusKey) => void;
}) {
  return (
      <div className={styles.statusKeysSection}>

        {/* Header row */}
        <div className={styles.sectionHeader}>
          <div className={styles.sectionLeft}>
            <h2 className={styles.sectionTitle}>Statuses</h2>
            {!keysLoading && visibleKeys.length > 0 && (
              <span className={styles.countChip}>{visibleKeys.length}</span>
            )}
            <div className={styles.segGroup}>
              {(['Active', 'Inactive'] as const).map(f => (
                <button
                  key={f}
                  className={`${styles.segBtn}${keyStatusFilter === f ? ` ${styles.segBtnActive}` : ''}`}
                  onClick={() => setKeyStatusFilter(f)}
                >
                  {f}
                </button>
              ))}
            </div>
          </div>
          {canEdit && (
            <button className={styles.btnPrimary} onClick={openCreateKey}>
              <PlusIcon /> Add Status
            </button>
          )}
        </div>

        {/* Two-column body */}
        <div className={styles.keysBody}>

          {/* LEFT — list */}
          <div className={styles.keysListCard}>
            {keysError ? (
              <div className={styles.errorAlert}><AlertIcon /> {keysError}</div>
            ) : keysLoading ? (
              <div className={styles.loadingCard}><SkeletonBlock /></div>
            ) : visibleKeys.length === 0 ? (
              <div className={styles.emptyCard}>
                <KeyIcon />
                <span className={styles.emptyTitle}>
                  {keyStatusFilter === 'Inactive' ? 'No inactive Statuses' : 'No Statuses yet'}
                </span>
                <span className={styles.emptyDesc}>
                  {keyStatusFilter === 'Inactive'
                    ? 'There are no deactivated Statuses.'
                    : 'Statuses are the entries a day can be marked with in payroll entry, such as Vacation, Sick Day or On Leave. They are shared by every Branch.'}
                </span>
                {canEdit && keyStatusFilter === 'Active' && statusKeys.length === 0 && (
                  <button
                    className={styles.btnPrimary}
                    onClick={openCreateKey}
                    style={{ marginTop: '1.25rem' }}
                  >
                    <PlusIcon /> Add First Status
                  </button>
                )}
              </div>
            ) : (
              <div className={styles.tableWrap}>
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Category</th>
                      <th className={styles.numTh}>Hours</th>
                      <th>Full day off</th>
                      <th>Paid through</th>
                      <th>State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleKeys.map(k => {
                      const isSelected = k.status_key_id === selectedKeyId;
                      const column = statusColumnName(columns, k.status_rate_column_id);
                      return (
                        <tr
                          key={k.status_key_id}
                          className={`${styles.tableRow}${isSelected ? ` ${styles.tableRowSelected}` : ''}${!k.is_active ? ` ${styles.rowInactive}` : ''}`}
                          onClick={() => dispatchKeys({ type: 'SELECT', id: k.status_key_id })}
                        >
                          <td>
                            <span className={styles.statusCodeLabel}>{k.key_name}</span>
                            {k.aliases.length > 0 && (
                              <small className={styles.aliasLine}>{k.aliases.join(', ')}</small>
                            )}
                          </td>
                          <td>{k.category}</td>
                          <td className={styles.numCell}>{Number(k.hours_value)}h</td>
                          <td>
                            {k.is_off_reason
                              ? <span className={`${styles.badge} ${styles.badgeBlue}`}>Yes</span>
                              : <span className={`${styles.badge} ${styles.badgeGray}`}>No</span>}
                          </td>
                          <td>{column ?? <span className={styles.mutedText}>Not paid</span>}</td>
                          <td>
                            {k.is_active
                              ? <span className={`${styles.badge} ${styles.badgeGreen}`}>Active</span>
                              : <span className={`${styles.badge} ${styles.badgeRed}`}>Inactive</span>}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* RIGHT — detail panel */}
          <div className={styles.keysDetailPanel}>
            {!selectedKey ? (
              <div className={styles.keysDetailEmpty}>
                <KeyIcon />
                <p className={styles.keysDetailEmptyTitle}>Choose a Status to show details</p>
                <p className={styles.keysDetailEmptyText}>
                  Select a Status from the list to view its configuration.
                </p>
              </div>
            ) : (
              <StatusKeyDetail
                statusKey={selectedKey}
                columns={columns}
                canEdit={canEdit}
                isDeactivating={deactivating === selectedKey.status_key_id}
                onEdit={() => openEditKey(selectedKey)}
                onDeactivate={() => requestDeactivate(selectedKey)}
                onReactivate={() => requestReactivate(selectedKey)}
                styles={styles}
              />
            )}
          </div>
        </div>
      </div>
  );
}
