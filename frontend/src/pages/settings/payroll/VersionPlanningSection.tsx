import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import { frequencyLabel } from '../../../lib/payrollSetupReadiness';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import type { DraftResponse, VersionResponse } from '../../../types/payrollSetup';
import {
  draftPlanningSummary,
  planningDateParts,
  planningDaysOffLabel,
} from './versionPlanningView';
import styles from './PayrollSetupsPage.module.css';

type DraftActionError = {
  draftId: number;
  info: ApiErrorInfo;
} | null;

type VersionPlanningSectionProps = {
  upcomingVersions: readonly VersionResponse[];
  drafts: readonly DraftResponse[];
  versionsLoading: boolean;
  versionsError: ApiErrorInfo | null;
  draftsLoading: boolean;
  draftsError: ApiErrorInfo | null;
  canCreate: boolean;
  mutating: boolean;
  draftActionError: DraftActionError;
  onCreate: () => void;
  onEditDraft: (draft: DraftResponse) => void;
  onDiscardDraft: (draft: DraftResponse) => void;
};

export function VersionPlanningSection({
  upcomingVersions,
  drafts,
  versionsLoading,
  versionsError,
  draftsLoading,
  draftsError,
  canCreate,
  mutating,
  draftActionError,
  onCreate,
  onEditDraft,
  onDiscardDraft,
}: VersionPlanningSectionProps) {
  const isResolvedEmpty =
    !versionsLoading &&
    !versionsError &&
    !draftsLoading &&
    !draftsError &&
    upcomingVersions.length === 0 &&
    drafts.length === 0;

  return (
    <section className={styles.versionPlanning} aria-labelledby="version-planning-title">
      <div className={styles.versionPlanningHeader}>
        <div>
          <h3 id="version-planning-title" className={styles.versionPlanningTitle}>Version Planning</h3>
          <p className={styles.versionPlanningSubtitle}>Prepare and schedule future payroll policy changes.</p>
        </div>
        {!isResolvedEmpty && canCreate && (
          <button className={styles.btnPrimary} onClick={onCreate} disabled={mutating}>
            Create new version
          </button>
        )}
      </div>

      {isResolvedEmpty ? (
        <div className={styles.versionPlanningEmpty}>
          <span className={styles.versionPlanningEmptyIcon} aria-hidden="true">＋</span>
          <p className={styles.versionPlanningEmptyTitle}>No versions planned yet</p>
          <p className={styles.versionPlanningEmptyText}>
            Create a new version now. You can save your work for later or publish it when you&apos;re ready.
          </p>
          {canCreate && (
            <button className={styles.btnPrimary} onClick={onCreate} disabled={mutating}>
              Create new version
            </button>
          )}
        </div>
      ) : (
        <div className={styles.versionPlanningContent}>
          {versionsError ? (
            <div className={styles.versionPlanningGroup}>
              <PayrollErrorNotice info={versionsError} />
            </div>
          ) : versionsLoading ? (
            <div className={styles.versionPlanningGroup}>
              <p className={styles.mutedText}>Loading upcoming versions…</p>
            </div>
          ) : upcomingVersions.length > 0 ? (
            <div className={styles.versionPlanningGroup}>
              <div className={styles.versionPlanningRows}>
                {upcomingVersions.map((version) => {
                  const date = planningDateParts(version.effective_from_date);
                  return (
                    <article key={version.version_id} className={styles.upcomingVersionRow}>
                      <div className={styles.upcomingVersionDate}>
                        {date ? (
                          <>
                            <span>{date.month}</span>
                            <strong>{date.day}</strong>
                            <span>{date.year}</span>
                          </>
                        ) : (
                          <strong className={styles.upcomingVersionDateFallback}>Date unavailable</strong>
                        )}
                      </div>
                      <div className={styles.upcomingVersionBody}>
                        <div className={styles.upcomingVersionTopline}>
                          <strong>{frequencyLabel(version.schedule.payroll_frequency)}</strong>
                        <span className={styles.scheduledBadge}>Scheduled update</span>
                        </div>
                        <p>{planningDaysOffLabel(version.schedule.normal_days_off_mask)}</p>
                        {date && <p className={styles.upcomingVersionEffective}>Takes effect {date.long}</p>}
                      </div>
                    </article>
                  );
                })}
              </div>
            </div>
          ) : null}

          {draftsError ? (
            <div className={styles.versionPlanningGroup}>
              <PayrollErrorNotice info={draftsError} />
            </div>
          ) : draftsLoading ? (
            <div className={styles.versionPlanningGroup}>
              <p className={styles.mutedText}>Loading saved versions…</p>
            </div>
          ) : drafts.length > 0 ? (
            <div className={styles.versionPlanningGroup}>
              <div className={styles.versionPlanningRows}>
                {drafts.map((draft) => (
                  <article key={draft.version_id} className={styles.planningDraftRow}>
                    <div className={styles.planningDraftBody}>
                      <div className={styles.planningDraftTopline}>
                        <span className={styles.draftBadge}>Draft</span>
                        <strong>{draftPlanningSummary(draft)}</strong>
                      </div>
                      <span className={styles.planningDraftMeta}>
                        Saved for later
                      </span>
                    </div>
                    <div className={styles.planningDraftActions}>
                      {canCreate && (
                        <>
                          <button
                            className={styles.btnSecondary}
                            onClick={() => onEditDraft(draft)}
                            disabled={mutating}
                          >
                            Continue editing
                          </button>
                          <button
                            className={styles.btnDanger}
                            onClick={() => onDiscardDraft(draft)}
                            disabled={mutating}
                          >
                            Discard
                          </button>
                        </>
                      )}
                    </div>
                    {draftActionError?.draftId === draft.version_id && (
                      <div className={styles.planningDraftError}>
                        <PayrollErrorNotice info={draftActionError.info} />
                      </div>
                    )}
                  </article>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}
