import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { EmployeeDetail } from '../../../types/workforce';
import { avatarInitials, fmtDate, fmtDateTime } from '../peoplePresentation';
import {
  accessAccountPath,
  accountFacts,
  assignmentRows,
  driverStateLabel,
  employeeActions,
  employmentLabel,
  payRatesPath,
  transferRequestPath,
  type WorkforceActionContext,
} from './workforceModel';
import styles from '../PeoplePage.module.css';

interface Props {
  detail: EmployeeDetail;
  context: WorkforceActionContext;
  onEdit(): void;
  onAddDriver(): void;
  onEditDriver(): void;
  onTerminate(): void;
}

/**
 * One Employee as the durable model: Employee → optional permanent Driver → DriverAssignments,
 * with the contextual Access summary only for an operator who holds Access authority.
 */
export function EmployeeDetailView({ detail, context, onEdit, onAddDriver, onEditDriver, onTerminate }: Props) {
  const actions = employeeActions(detail, context);
  // Fixed per mounted Employee; the page remounts this view when another Employee is selected.
  const [now] = useState(() => Date.now());
  const assignments = assignmentRows(detail, now);
  const payRates = payRatesPath(detail);
  const transfer = transferRequestPath(detail);
  const account = accountFacts(detail.linked_user);

  return (
    <div className={styles.detail}>
      <div className={styles.dHeaderCard}>
        <div className={styles.dAv}>{avatarInitials(detail.full_name)}</div>
        <div className={styles.dHeaderInfo}>
          <div className={styles.dName}>
            {detail.full_name}
            {detail.driver && <span className={styles.driverPill}>Driver</span>}
          </div>
          <div className={styles.dSub}>{detail.employee_key ?? `Employee ${detail.employee_id}`} · {detail.branch_name}</div>
          <div className={styles.dBadges}>
            <span className={detail.employment_status === 'Active' ? styles.badgeOk : styles.badgeErr}>{employmentLabel(detail.employment_status)}</span>
            <span className={styles.asBadge}>{driverStateLabel(detail)}</span>
          </div>
          <div className={styles.dActions}>
            {actions.edit && <button className={styles.actBtn} onClick={onEdit}>Edit Employee</button>}
            {actions.addDriver && <button className={`${styles.actBtn} ${styles.actBtnDriver}`} onClick={onAddDriver}>Add Driver identity</button>}
            {actions.requestTransfer && transfer && <Link className={styles.actBtn} to={transfer}>Request Transfer</Link>}
            {actions.payRates && payRates && <Link className={`${styles.actBtn} ${styles.actBtnDriver}`} to={payRates}>Pay Rates →</Link>}
            {actions.terminate && <button className={`${styles.actBtn} ${styles.actBtnDanger}`} onClick={onTerminate}>Terminate</button>}
          </div>
        </div>
      </div>

      <div className={styles.sections}>
        <section className={styles.sec}>
          <h3 className={styles.secTitle}>Employee</h3>
          <div className={styles.grid2}>
            <span className={styles.fl}>Employment status</span><span>{detail.employment_status}</span>
            <span className={styles.fl}>Hire date</span><span>{fmtDate(detail.hire_date)}</span>
            {detail.termination_date && (<><span className={styles.fl}>Termination date</span><span>{fmtDate(detail.termination_date)}</span></>)}
            <span className={styles.fl}>Email</span><span>{detail.email ?? '—'}</span>
            <span className={styles.fl}>Phone</span><span>{detail.primary_phone ?? '—'}</span>
            <span className={styles.fl}>Branch</span><span>{detail.branch_name}</span>
          </div>
        </section>

        <section className={styles.sec}>
          <div className={styles.secTitleRow}>
            <h3 className={styles.secTitle}>Driver identity</h3>
            {actions.editDriver && <button className={styles.secEditBtn} onClick={onEditDriver}>Edit</button>}
          </div>
          {detail.driver ? (
            <div className={styles.grid2}>
              <span className={styles.fl}>Driver number</span><span>{detail.driver.driver_number ?? '—'}</span>
              <span className={styles.fl}>CDL number</span><span>{detail.driver.cdl_number ?? '—'}</span>
              <span className={styles.fl}>Driver work status</span><span>{detail.driver.driver_status}</span>
            </div>
          ) : (
            <p className={styles.emptySecMsg}>This Employee is not a Driver.</p>
          )}
        </section>

        {detail.driver && (
          <section className={styles.sec}>
            <h3 className={styles.secTitle}>Home assignment</h3>
            {detail.current_or_pending_assignment ? (
              <div className={styles.grid2}>
                <span className={styles.fl}>{detail.driver_state === 'pending' ? 'Scheduled assignment' : 'Current assignment'}</span>
                <span>{detail.current_or_pending_assignment.branch_name}</span>
                <span className={styles.fl}>Starts</span><span>{fmtDateTime(detail.current_or_pending_assignment.effective_from_at)}</span>
                <span className={styles.fl}>Ends</span><span>{detail.current_or_pending_assignment.effective_to_at ? fmtDateTime(detail.current_or_pending_assignment.effective_to_at) : 'Open-ended'}</span>
              </div>
            ) : (
              <p className={styles.emptySecMsg}>No current or scheduled assignment.</p>
            )}
            <p className={styles.helpTxt}>The home Branch of a Driver changes only through a transfer request.</p>
          </section>
        )}

        {detail.driver && assignments.length > 0 && (
          <section className={styles.sec}>
            <h3 className={styles.secTitle}>Assignment history</h3>
            <table style={{ width: '100%', fontSize: '0.8rem', borderCollapse: 'collapse' }}>
              <thead><tr><th align="left">Branch</th><th align="left">From</th><th align="left">To</th><th align="left">State</th><th align="left">Ended by</th></tr></thead>
              <tbody>
                {assignments.map((a) => (
                  <tr key={a.driver_assignment_id}>
                    <td>{a.branch_name}</td>
                    <td>{fmtDateTime(a.effective_from_at)}</td>
                    <td>{a.effective_to_at ? fmtDateTime(a.effective_to_at) : '—'}</td>
                    <td>{a.state === 'current' ? 'Current' : a.state === 'pending' ? 'Scheduled' : 'Ended'}</td>
                    <td>{a.end_reason ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}

        {actions.showAccess && (
          <section className={styles.sec}>
            <h3 className={styles.secTitle}>Access</h3>
            {account.linked && detail.linked_user ? (
              <div className={styles.grid2}>
                <span className={styles.fl}>Account</span><span>{detail.linked_user.display_name} (@{detail.linked_user.username})</span>
                <span className={styles.fl}>Provisioning</span><span>{account.lifecycle}</span>
                <span className={styles.fl}>Account state</span><span>{account.active}</span>
                <span className={styles.fl}>Login</span><span>{account.login}</span>
                {actions.openAccount && (
                  <><span className={styles.fl}>Manage</span><span><Link className={styles.linkBtn} to={accessAccountPath(detail.linked_user)}>Open account in Access →</Link></span></>
                )}
              </div>
            ) : (
              <>
                <p className={styles.emptySecMsg}>No account is linked to this Employee.</p>
                {actions.createAccount && (
                  <Link className={styles.actBtn} to={`/people/access?newForEmployee=${detail.employee_id}`}>Create account for this Employee</Link>
                )}
                {!actions.createAccount && actions.linkAccount && (
                  <p className={styles.helpTxt}>Link an existing account from its page in Access.</p>
                )}
              </>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
