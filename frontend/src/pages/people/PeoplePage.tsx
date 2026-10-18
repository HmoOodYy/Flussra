import { usePeopleController } from './usePeopleController';
import { accessStatus, avatarInitials, scopeLabel } from './peoplePresentation';
import type { FilterStatus } from './peoplePageState';
import { PersonDetail } from './PersonDetail';
import { PeopleAccountDialogs } from './PeopleAccountDialogs';
import { WizardModal } from './PeopleWizard';
import { TransferRequestsTab } from './TransferRequestsTab';
import styles from './PeoplePage.module.css';

export function PeoplePage() {
  const {
    st, dispatch, navigate, filtered, selected, amOwner, userCanCreate, userCanEditPeople,
    userCanToggle, userCanChangeRole, userCanTransfers, assignableRoles, ownerTransferRoles,
    wizStep1, wizSelectRole, wizStep3, wizStep4, wizFinish, submitEdit, submitReset,
    confirmToggleActive, submitChangeRole, submitExtraPerms, submitTransfer,
  } = usePeopleController();
  if (st.loading) return <div className={styles.splash}>Loading people…</div>;
  if (st.error)   return <div className={styles.splash} style={{ color: '#dc2626' }}>{st.error}</div>;

  const targetPerson = st.transfer.targetId ? st.people.find(p => p.user_id === st.transfer.targetId) : null;

  return (
    <div className={styles.page}>
      {st.toast && <div className={styles.toast}>{st.toast}</div>}

      {/* ── Header ── */}
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>People</h1>
          <p className={styles.sub}>Add and manage everyone who can be assigned roles, access, or pay setup.</p>
        </div>
        {st.tab === 'people' && userCanCreate && (
          <button className={styles.addBtn} onClick={() => dispatch({ type: 'WIZ_OPEN' })}>+ Add Person</button>
        )}
      </div>

      {/* ── Tab bar ── */}
      <div className={styles.tabBar}>
        <button
          className={`${styles.tabBtn}${st.tab === 'people' ? ` ${styles.tabBtnActive}` : ''}`}
          onClick={() => dispatch({ type: 'SET_TAB', tab: 'people' })}
        >
          People
        </button>
        {userCanTransfers && (
          <button
            className={`${styles.tabBtn}${st.tab === 'transfers' ? ` ${styles.tabBtnActive}` : ''}`}
            onClick={() => dispatch({ type: 'SET_TAB', tab: 'transfers' })}
          >
            Transfer Requests
          </button>
        )}
      </div>

      {/* ── Transfer Requests tab ── */}
      {st.tab === 'transfers' && userCanTransfers && (
        <div className={styles.body}>
          <TransferRequestsTab branches={st.branches} />
        </div>
      )}

      {/* ── People tab body ── */}
      {st.tab === 'people' && <div className={styles.body}>

        {/* ── Left panel ── */}
        <aside className={styles.left}>
          {/* Filters */}
          <div className={styles.filters}>
            <input className={styles.search} placeholder="Search name, username, email…" value={st.search} onChange={e => dispatch({ type: 'SET_SEARCH', val: e.target.value })} />
            <div className={styles.filterRow}>
              <select className={styles.sel} value={st.filterStatus} onChange={e => dispatch({ type: 'SET_FILTER_STATUS', val: e.target.value as FilterStatus })}>
                <option value="active">Active</option>
                <option value="inactive">Inactive</option>
                <option value="all">All statuses</option>
              </select>
              <select className={styles.sel} value={st.filterRoleId ?? ''} onChange={e => dispatch({ type: 'SET_FILTER_ROLE', val: e.target.value ? parseInt(e.target.value) : null })}>
                <option value="">All roles</option>
                {st.roles.map(r => <option key={r.company_role_id} value={r.company_role_id}>{r.role_name}</option>)}
              </select>
            </div>
          </div>

          {/* List */}
          <div className={styles.list}>
            {filtered.length === 0 && <div className={styles.emptyList}>No people match your filters.</div>}
            {filtered.map(p => {
              const as = accessStatus(p);
              const isOwner = p.company_role_code === 'COMPANY_OWNER';
              return (
                <button key={p.user_id} className={`${styles.item}${st.selectedId === p.user_id ? ` ${styles.itemActive}` : ''}`} onClick={() => dispatch({ type: 'SELECT', id: p.user_id })}>
                  <div className={`${styles.av}${isOwner ? ` ${styles.avOwner}` : ''}`}>{avatarInitials(p.display_name)}</div>
                  <div className={styles.itemBody}>
                    <div className={styles.itemName}>
                      {p.display_name}
                      {isOwner && <span className={styles.ownerPill}>Owner</span>}
                      {!p.is_active && <span className={styles.pill} style={{ background: '#fee2e2', color: '#991b1b' }}>Inactive</span>}
                    </div>
                    <div className={styles.itemMeta}>{p.username}{p.email ? ` · ${p.email}` : ''}</div>
                    <div className={styles.itemRow}>
                      {p.company_role_name
                        ? <span className={styles.roleChip}>{p.company_role_name}</span>
                        : <span className={styles.noRole}>No role</span>
                      }
                      {p.company_role_scope && (
                        <span className={styles.scopeChip}>{scopeLabel(p.company_role_scope, p.company_role_branch_name)}</span>
                      )}
                    </div>
                  </div>
                  <div className={styles.itemRight}>
                    <span className={`${styles.asBadge} ${styles[`as_${as.kind}`]}`}>{as.label}</span>
                    {p.can_login && p.is_active && <span className={styles.loginDot} title="Can login" />}
                  </div>
                </button>
              );
            })}
          </div>
          <div className={styles.listFoot}>{filtered.length} / {st.people.length} people</div>
        </aside>

        {/* ── Right panel ── */}
        <main className={styles.right}>
          {!selected ? (
            <div className={styles.emptyRight}>
              <div className={styles.emptyRightCard}>
                <div className={styles.emptyRightIcon}>
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>
                    <path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
                  </svg>
                </div>
                <p className={styles.emptyRightTitle}>No person selected</p>
                <p className={styles.emptyRightMsg}>Choose a person from the list to view their access, role, and permissions.</p>
              </div>
            </div>
          ) : (
            <PersonDetail
              person={selected}
              roles={st.roles}
              branches={st.branches}
              allPerms={st.allPerms}
              amOwner={amOwner}
              canEditProfile={userCanEditPeople}
              canToggleActive={userCanToggle}
              canResetPassword={userCanEditPeople}
              canChangeRole={userCanChangeRole}
              canEditExtraPermissions={userCanEditPeople}
              onEdit={() => dispatch({ type: 'EDIT_OPEN', person: selected })}
              onToggleActive={() => dispatch({ type: 'CONFIRM_TOGGLE', userId: selected.user_id, toActive: !selected.is_active })}
              onResetPw={() => dispatch({ type: 'RESET_OPEN' })}
              onChangeRole={() => dispatch({ type: 'CR_OPEN', person: selected })}
              onEditExtraPerms={() => dispatch({ type: 'EP_OPEN', person: selected })}
              onTransfer={() => dispatch({ type: 'TRANSFER_OPEN', targetId: selected.user_id })}
              onGoPayRates={() => navigate(
                // Pay Rates is assignment-addressed; the user lookup resolves the Driver's
                // current assignment.
                `/people/pay-rates?driverUserId=${selected.user_id}`
              )}
            />
          )}
        </main>
      </div>}

      {/* ── Modals ── */}

      {st.wiz.open && (
        <WizardModal
          wiz={st.wiz} roles={assignableRoles} branches={st.branches} allPerms={st.allPerms}
          dispatch={dispatch} onStep1={wizStep1} onSelectRole={wizSelectRole} onStep3={wizStep3} onStep4={wizStep4} onFinish={wizFinish}
        />
      )}

      <PeopleAccountDialogs
        st={st} selected={selected} targetPerson={targetPerson}
        assignableRoles={assignableRoles} ownerTransferRoles={ownerTransferRoles} dispatch={dispatch}
        submitEdit={submitEdit} submitReset={submitReset} submitChangeRole={submitChangeRole}
        submitExtraPerms={submitExtraPerms} confirmToggleActive={confirmToggleActive} submitTransfer={submitTransfer}
      />
    </div>
  );
}
