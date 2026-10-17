import { SectionCard } from '../../../components/ui';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import { friendlyScheduleSummary } from '../../../lib/payrollSetupReadiness';
import { formatIsoLong } from '../../../lib/isoDate';
import { draftCreatedLabel, versionRelationship } from './payrollSetupsView';
import type { PolicyBranchRow, PolicyNextStepKind } from './payrollSetupsView';
import { isDraftComplete } from './draftEditor';
import { PublishPanel } from './PublishPanel';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import type { DraftResponse, SetupResponse, VersionResponse } from '../../../types/payrollSetup';
import type { ActionError, ConfirmKind, DraftActionError } from './policyDetailTypes';
import styles from './PayrollSetupsPage.module.css';

type Props = {
  selectedPolicy: SetupResponse | null;
  selectedPolicyId: number | null;
  nextSteps: PolicyNextStepKind[];
  nextStepText: Record<PolicyNextStepKind, string>;
  nextStepPermitted: Record<PolicyNextStepKind, boolean>;
  handleNextStepAction: (kind: PolicyNextStepKind) => void;
  openConfirm: (kind: ConfirmKind) => void;
  mutating: boolean;
  canManage: boolean;
  canPublish: boolean;
  archiveReason: string | null;
  actionError: ActionError | null;
  drafts: DraftResponse[];
  draftsError: ApiErrorInfo | null;
  draftsLoading: boolean;
  openCreateDraftModal: () => void;
  openEditDraftModal: (draft: DraftResponse) => void;
  openDiscardConfirm: (draft: DraftResponse) => void;
  draftActionError: DraftActionError | null;
  publishDraft: DraftResponse | null;
  currentPublishDraft: DraftResponse | null;
  setPublishDraft: (draft: DraftResponse | null) => void;
  setSetupDataReloadKey: React.Dispatch<React.SetStateAction<number>>;
  branchDirectory: { branch_id: number; branch_name: string }[];
  setMutating: (value: boolean) => void;
  showToast: (message: string) => void;
  versions: VersionResponse[];
  displayVersionNumbers: ReadonlyMap<number, number>;
  versionsError: ApiErrorInfo | null;
  versionsLoading: boolean;
  summariesError: ApiErrorInfo | null;
  summariesLoading: boolean;
  assignedBranchRows: PolicyBranchRow[];
};

export function PolicyDetailLowerSections(p: Props) {
  const {
    selectedPolicy,
    selectedPolicyId,
    nextSteps,
    nextStepText,
    nextStepPermitted,
    handleNextStepAction,
    openConfirm,
    mutating,
    canManage,
    canPublish,
    archiveReason,
    actionError,
    drafts,
    draftsError,
    draftsLoading,
    openCreateDraftModal,
    openEditDraftModal,
    openDiscardConfirm,
    draftActionError,
    publishDraft,
    currentPublishDraft,
    setPublishDraft,
    setSetupDataReloadKey,
    branchDirectory,
    setMutating,
    showToast,
    versions,
    displayVersionNumbers,
    versionsError,
    versionsLoading,
    summariesError,
    summariesLoading,
    assignedBranchRows,
  } = p;
  return (
    <>
      {/* ── Parked: policy detail lower block (next steps, archive) ── */}
      {selectedPolicy && (
        <div className={styles.setupsDetailPanel}>
          <div className={styles.detailContent}>
            {nextSteps.length > 0 && (
              <div className={styles.previewResult}>
                <p className={styles.label}>Next steps</p>
                <ul className={styles.draftList}>
                  {nextSteps.map((kind) => (
                    <li key={kind} className={styles.assignmentMeta}>
                      {nextStepText[kind]}
                      {nextStepPermitted[kind] && (
                        <button
                          className={`${styles.btnSecondary} ${styles.nextStepAction}`}
                          onClick={() => handleNextStepAction(kind)}
                          disabled={mutating}
                        >
                          Go
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {selectedPolicy.status === 'Active' ? (
              <div className={styles.detailActions}>
                {canManage && (
                  archiveReason ? (
                    <span className={styles.archiveBlocked}>
                      <button className={styles.btnDanger} disabled>
                        Archive
                      </button>
                      <span className={styles.archiveBlockedReason}>{archiveReason}</span>
                    </span>
                  ) : (
                    <button className={styles.btnDanger} onClick={() => openConfirm('archive')} disabled={mutating}>
                      Archive
                    </button>
                  )
                )}
              </div>
            ) : (
              <p className={styles.mutedNote}>Archived policies are read-only.</p>
            )}

            {actionError?.kind === 'archive' && actionError.setupId === selectedPolicyId && (
              <PayrollErrorNotice info={actionError.info} />
            )}

            <details className={styles.mutedNote}>
              <summary>Integration details</summary>
              <p>{`Reference code: ${selectedPolicy.setup_code}`}</p>
            </details>
          </div>
        </div>
      )}

      {/* ── Drafts ── */}
      {selectedPolicy && (
        <SectionCard
          title="Draft schedules"
          actions={
            selectedPolicy.status === 'Active' && canManage ? (
              <button className={styles.btnPrimary} onClick={openCreateDraftModal} disabled={mutating}>
                + Add draft schedule
              </button>
            ) : undefined
          }
        >
          {draftsError ? (
            <PayrollErrorNotice info={draftsError} />
          ) : draftsLoading ? (
            <p className={styles.mutedText}>Loading…</p>
          ) : drafts.length === 0 ? (
            <p className={styles.mutedText}>No draft schedules.</p>
          ) : (
            <ul className={styles.draftList}>
              {drafts.map((d) => {
                const complete = isDraftComplete(d);
                return (
                  <li key={d.version_id} className={styles.draftRow}>
                    <div className={styles.draftRowMain}>
                      <span className={styles.draftRowTitle}>{draftCreatedLabel(d.created_at_utc)}</span>
                      <span className={styles.draftRowSchedule}>
                        {complete
                          ? friendlyScheduleSummary({
                              payroll_frequency: d.payroll_frequency as string,
                              anchor_start_date: d.anchor_start_date as string,
                              custom_interval_days: d.custom_interval_days,
                              normal_days_off_mask: d.normal_days_off_mask as number,
                            })
                          : 'Incomplete schedule'}
                      </span>
                    </div>

                    {selectedPolicy.status === 'Active' && (
                      <div className={styles.draftRowActions}>
                        {canManage && (
                          <button
                            className={styles.btnSecondary}
                            onClick={() => openEditDraftModal(d)}
                            disabled={mutating}
                          >
                            Edit
                          </button>
                        )}
                        {canManage && (
                          <button
                            className={styles.btnDanger}
                            onClick={() => openDiscardConfirm(d)}
                            disabled={mutating}
                          >
                            Discard
                          </button>
                        )}
                        {canPublish && (
                          <span className={styles.archiveBlocked}>
                            <button
                              className={styles.btnSecondary}
                              onClick={() => setPublishDraft(d)}
                              disabled={mutating || !complete}
                            >
                              Publish…
                            </button>
                            {!complete && (
                              <span className={styles.archiveBlockedReason}>
                                Complete the schedule before publishing.
                              </span>
                            )}
                          </span>
                        )}
                      </div>
                    )}

                    {draftActionError?.kind === 'discard' &&
                      draftActionError.setupId === selectedPolicy.setup_id &&
                      draftActionError.draftId === d.version_id && (
                        <PayrollErrorNotice info={draftActionError.info} />
                      )}
                  </li>
                );
              })}
            </ul>
          )}
        </SectionCard>
      )}

      {/* ── Publish panel (preview-first) ── */}
      {selectedPolicy && selectedPolicy.status === 'Active' && canPublish && publishDraft && currentPublishDraft && (
        <SectionCard title="Publish">
          <PublishPanel
            key={currentPublishDraft.version_id}
            setupId={selectedPolicy.setup_id}
            draft={currentPublishDraft}
            branches={branchDirectory}
            mutating={mutating}
            setMutating={setMutating}
            onClose={() => setPublishDraft(null)}
            onPublished={() => {
              setPublishDraft(null);
              setSetupDataReloadKey((k) => k + 1);
            }}
            showToast={showToast}
          />
        </SectionCard>
      )}

      {/* ── Policy updates (read-only) ── */}
      <SectionCard title="Policy updates">
        {!selectedPolicy ? (
          <p className={styles.mutedText}>Select a policy to view its updates.</p>
        ) : versionsError ? (
          <PayrollErrorNotice info={versionsError} />
        ) : versionsLoading ? (
          <p className={styles.mutedText}>Loading…</p>
        ) : versions.length === 0 ? (
          <p className={styles.mutedText}>No published updates yet.</p>
        ) : (
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Starts on</th>
                  <th>Next change</th>
                  <th>Payroll schedule</th>
                  <th>Relationship</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.version_id}>
                    <td>
                      {displayVersionNumbers.has(v.version_id)
                        ? `Version ${displayVersionNumbers.get(v.version_id)}`
                        : v.is_terminal
                          ? 'Scheduled update'
                          : 'Published correction'}
                    </td>
                    <td>{formatIsoLong(v.effective_from_date)}</td>
                    <td>{v.effective_to_date ? formatIsoLong(v.effective_to_date) : 'No scheduled policy change'}</td>
                    <td>{friendlyScheduleSummary(v.schedule)}</td>
                    <td>{versionRelationship(v, versions) ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

      {/* ── Assigned branches ── */}
      <SectionCard title="Assigned branches">
        {!selectedPolicy ? (
          <p className={styles.mutedText}>Select a policy to view its assigned branches.</p>
        ) : summariesError ? (
          <PayrollErrorNotice info={summariesError} />
        ) : summariesLoading ? (
          <p className={styles.mutedText}>Loading…</p>
        ) : assignedBranchRows.length === 0 ? (
          <p className={styles.mutedText}>No branches follow this policy yet.</p>
        ) : (
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Branch</th>
                  <th>Relation</th>
                  <th>Since</th>
                </tr>
              </thead>
              <tbody>
                {assignedBranchRows.map((row) => (
                  <tr key={`${row.branch_id}-${row.relation}`}>
                    <td>
                      {row.branch_name}
                      {row.branch_code && ` (${row.branch_code})`}
                    </td>
                    <td>{row.relation === 'current' ? 'Current' : 'Scheduled'}</td>
                    <td>{formatIsoLong(row.since_date)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

    </>
  );
}
