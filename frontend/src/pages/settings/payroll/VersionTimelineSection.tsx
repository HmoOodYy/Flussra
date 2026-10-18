import { useMemo } from 'react';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import type { VersionResponse } from '../../../types/payrollSetup';
import { effectiveDisplayVersionNumbers, timelineDateRange, timelinePublishedVersions, timelineScheduleSummary } from './policyDetailView';
import styles from './PayrollSetupsPage.module.css';

type VersionTimelineSectionProps = {
  versions: readonly VersionResponse[];
  loading: boolean;
  error: ApiErrorInfo | null;
};

export function VersionTimelineSection({ versions, loading, error }: VersionTimelineSectionProps) {
  const timelineVersions = useMemo(() => timelinePublishedVersions(versions), [versions]);
  const displayNumbers = useMemo(() => effectiveDisplayVersionNumbers(versions), [versions]);

  return (
    <section className={styles.versionTimeline} aria-labelledby="version-timeline-title">
      <div className={styles.versionTimelineHeader}>
        <div>
          <h3 id="version-timeline-title" className={styles.versionTimelineTitle}>Version Timeline</h3>
          <p className={styles.versionTimelineSubtitle}>See the published policy changes that have taken effect.</p>
        </div>
      </div>
      {error ? (
        <div className={styles.versionTimelineMessage}>
          <PayrollErrorNotice info={error} />
        </div>
      ) : loading ? (
        <p className={styles.versionTimelineMessage}>Loading published versions…</p>
      ) : timelineVersions.length === 0 ? (
        <p className={styles.versionTimelineMessage}>No version has taken effect yet.</p>
      ) : (
        <ol className={styles.versionTimelineList}>
          {timelineVersions.map((version) => (
            <li key={version.version_id} className={styles.versionTimelineItem}>
              <span className={styles.versionTimelineNode} aria-hidden="true" />
              <article className={styles.versionTimelineCard}>
                <div className={styles.versionTimelineCardHeader}>
                  <div className={styles.versionTimelineVersion}>
                    <strong>{`Version ${displayNumbers.get(version.version_id) ?? '—'}`}</strong>
                    {version.is_current && <span className={styles.versionTimelineCurrent}>Current</span>}
                  </div>
                  <span className={styles.versionTimelineDate}>{timelineDateRange(version)}</span>
                </div>
                <p className={styles.versionTimelineSummary}>{timelineScheduleSummary(version)}</p>
              </article>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
