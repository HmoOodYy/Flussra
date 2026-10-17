import type { Dispatch, SetStateAction } from 'react';
import type { StatusKey } from '../../../types/settings';
import type { KeysLoadAction } from './statusKeysModel';
import { StatusKeyDetail } from './StatusKeyDetail';
import { AlertIcon, KeyIcon, PlusIcon, SkeletonBlock } from './StatusKeysDisplay';
import styles from './StatusKeysPage.module.css';

export function StatusKeysWorkspace({
  statusKeys, keysLoading, keysError, keyStatusFilter, setKeyStatusFilter, canEdit, openCreateKey,
  selectedKeyId, dispatchKeys, selectedKey, deactivating, openEditKey, requestDeactivate, requestReactivate,
}: {
  statusKeys: StatusKey[];
  keysLoading: boolean;
  keysError: string;
  keyStatusFilter: 'Active' | 'Inactive';
  setKeyStatusFilter: Dispatch<SetStateAction<'Active' | 'Inactive'>>;
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
            <h2 className={styles.sectionTitle}>Status Keys</h2>
            {!keysLoading && statusKeys.length > 0 && (
              <span className={styles.countChip}>{statusKeys.length}</span>
            )}
            {/* Segmented status filter replaces the old checkbox */}
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
              <PlusIcon /> Add Status Key
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
            ) : statusKeys.length === 0 ? (
              <div className={styles.emptyCard}>
                <KeyIcon />
                <span className={styles.emptyTitle}>
                  {keyStatusFilter === 'Inactive' ? 'No inactive status keys' : 'No status keys yet'}
                </span>
                <span className={styles.emptyDesc}>
                  {keyStatusFilter === 'Inactive'
                    ? 'There are no deactivated status keys for this branch.'
                    : 'Status keys define payroll entry types such as Vacation, Sick Day, or On Leave.'}
                </span>
                {canEdit && keyStatusFilter === 'Active' && (
                  <button
                    className={styles.btnPrimary}
                    onClick={openCreateKey}
                    style={{ marginTop: '1.25rem' }}
                  >
                    <PlusIcon /> Add First Status Key
                  </button>
                )}
              </div>
            ) : (
              <div className={styles.tableWrap}>
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th>Key Name</th>
                      <th className={styles.numTh}>Hours</th>
                      <th>Leave/Off</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {statusKeys.map(k => {
                      const isSelected = k.status_key_id === selectedKeyId;
                      return (
                        <tr
                          key={k.status_key_id}
                          className={`${styles.tableRow}${isSelected ? ` ${styles.tableRowSelected}` : ''}${!k.is_active ? ` ${styles.rowInactive}` : ''}`}
                          onClick={() => dispatchKeys({ type: 'SELECT', id: k.status_key_id })}
                        >
                          <td><span className={styles.statusCodeLabel}>{k.key_name}</span></td>
                          <td className={styles.numCell}>{k.hours_value}h</td>
                          <td>
                            {k.is_off_reason
                              ? <span className={`${styles.badge} ${styles.badgeBlue}`}>Yes</span>
                              : <span className={`${styles.badge} ${styles.badgeGray}`}>No</span>}
                          </td>
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
                <p className={styles.keysDetailEmptyTitle}>Choose a status key to show details</p>
                <p className={styles.keysDetailEmptyText}>
                  Select a key from the list to view its configuration and settings.
                </p>
              </div>
            ) : (
              <StatusKeyDetail
                statusKey={selectedKey}
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
