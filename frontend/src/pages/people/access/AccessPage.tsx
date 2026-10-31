import { canViewPayRates } from '../../../lib/permissions';
import { useAuth } from '../../../store/authStore';
import { avatarInitials, scopeLabel } from '../peoplePresentation';
import { AccessDetail } from './AccessDetail';
import { AccessDialogs } from './AccessDialogs';
import { accessStatus, type LinkFilter, type StatusFilter } from './accessModel';
import { useAccessController } from './useAccessController';
import styles from '../PeoplePage.module.css';

/** Access: User accounts (login, role, scope, overrides, Employee link), including Users with no Employee. */
export function AccessPage() {
  const { user: authUser } = useAuth();
  const c = useAccessController();
  const { st, dispatch } = c;
  if (st.loading) return <div className={styles.splash}>Loading accounts…</div>;
  if (st.error) return <div className={styles.splash} style={{ color: '#dc2626' }}>{st.error}</div>;

  const ownerTransferTarget = st.transfer.targetId ? st.users.find(u => u.user_id === st.transfer.targetId) : null;

  return (
    <div className={styles.body}>
      {st.toast && <div className={styles.toast}>{st.toast}</div>}

      <aside className={styles.left}>
        <div className={styles.filters}>
          {c.can.create && (
            <button className={styles.addBtn} onClick={() => dispatch({ type: 'CREATE_OPEN' })}>+ Create account</button>
          )}
          <input className={styles.search} placeholder="Search name, username, email, Employee…" value={st.filters.search} onChange={e => dispatch({ type: 'FILTERS', filters: { search: e.target.value } })} />
          <div className={styles.filterRow}>
            <select className={styles.sel} aria-label="Account state" value={st.filters.status} onChange={e => dispatch({ type: 'FILTERS', filters: { status: e.target.value as StatusFilter } })}>
              <option value="active">Active accounts</option>
              <option value="inactive">Inactive accounts</option>
              <option value="all">All accounts</option>
            </select>
            <select className={styles.sel} aria-label="Role" value={st.filters.roleId ?? ''} onChange={e => dispatch({ type: 'FILTERS', filters: { roleId: e.target.value ? parseInt(e.target.value) : null } })}>
              <option value="">All roles</option>
              {st.roles.map(r => <option key={r.company_role_id} value={r.company_role_id}>{r.role_name}</option>)}
            </select>
          </div>
          <select className={styles.sel} aria-label="Employee link" value={st.filters.link} onChange={e => dispatch({ type: 'FILTERS', filters: { link: e.target.value as LinkFilter } })}>
            <option value="all">Linked and stand-alone</option>
            <option value="linked">Linked to an Employee</option>
            <option value="unlinked">No Employee (stand-alone)</option>
            <option value="staged">Staged, not provisioned</option>
          </select>
        </div>

        <div className={styles.list}>
          {c.filtered.length === 0 && <div className={styles.emptyList}>No accounts match your filters.</div>}
          {c.filtered.map(u => {
            const status = accessStatus(u);
            const isOwner = u.company_role_code === 'COMPANY_OWNER';
            return (
              <button key={u.user_id} className={`${styles.item}${st.selectedId === u.user_id ? ` ${styles.itemActive}` : ''}`} onClick={() => c.select(u.user_id)}>
                <div className={`${styles.av}${isOwner ? ` ${styles.avOwner}` : ''}`}>{avatarInitials(u.display_name)}</div>
                <div className={styles.itemBody}>
                  <div className={styles.itemName}>
                    {u.display_name}
                    {isOwner && <span className={styles.ownerPill}>Owner</span>}
                    {!u.is_active && <span className={styles.pill} style={{ background: '#fee2e2', color: '#991b1b' }}>Inactive</span>}
                  </div>
                  <div className={styles.itemMeta}>{u.username}{u.email ? ` · ${u.email}` : ''}</div>
                  <div className={styles.itemRow}>
                    {u.company_role_name ? <span className={styles.roleChip}>{u.company_role_name}</span> : <span className={styles.noRole}>No role</span>}
                    {u.company_role_scope && <span className={styles.scopeChip}>{scopeLabel(u.company_role_scope, u.company_role_branch_name)}</span>}
                    <span className={styles.scopeChip}>{u.employee_id !== null ? `Employee: ${u.employee_name ?? u.employee_id}` : 'No Employee'}</span>
                  </div>
                </div>
                <div className={styles.itemRight}>
                  <span className={`${styles.asBadge} ${styles[`as_${status.kind}`]}`}>{status.label}</span>
                  {u.can_login && u.is_active && <span className={styles.loginDot} title="Can login" />}
                </div>
              </button>
            );
          })}
        </div>
        <div className={styles.listFoot}>{c.filtered.length} / {st.users.length} accounts</div>
      </aside>

      <main className={styles.right}>
        {!c.selected ? (
          <div className={styles.emptyRight}>
            <div className={styles.emptyRightCard}>
              <p className={styles.emptyRightTitle}>No account selected</p>
              <p className={styles.emptyRightMsg}>Choose an account from the list to view its login state, role, scope and Employee link.</p>
            </div>
          </div>
        ) : (
          <AccessDetail
            user={c.selected}
            allPerms={st.allPerms}
            amOwner={c.amOwner}
            isSelf={authUser?.user_id === c.selected.user_id}
            can={c.can}
            canViewPayRates={authUser ? canViewPayRates(authUser) : false}
            onEdit={() => dispatch({ type: 'EDIT_OPEN', user: c.selected! })}
            onToggleActive={() => dispatch({ type: 'CONFIRM_TOGGLE', userId: c.selected!.user_id, toActive: !c.selected!.is_active })}
            onResetPw={() => dispatch({ type: 'RESET_OPEN' })}
            onChangeRole={() => dispatch({ type: 'CR_OPEN', user: c.selected! })}
            onEditExtraPerms={() => dispatch({ type: 'EP_OPEN', user: c.selected! })}
            onOwnerTransfer={() => dispatch({ type: 'TRANSFER_OPEN', targetId: c.selected!.user_id })}
            onProvision={() => dispatch({ type: 'PROVISION_OPEN' })}
            onLink={() => dispatch({ type: 'LINK_OPEN' })}
            onUnlink={() => void c.unlink()}
          />
        )}
      </main>

      <AccessDialogs
        st={st} selected={c.selected} ownerTransferTarget={ownerTransferTarget}
        assignableRoles={c.assignableRoles} ownerTransferRoles={c.ownerTransferRoles} dispatch={dispatch}
        submitCreate={c.submitCreate} submitProvision={c.submitProvision} submitLink={c.submitLink}
        submitEdit={c.submitEdit} submitReset={c.submitReset} submitChangeRole={c.submitChangeRole}
        submitExtraPerms={c.submitExtraPerms} confirmToggleActive={c.confirmToggleActive}
        submitOwnerTransfer={c.submitOwnerTransfer} loadMoreCandidates={c.loadMoreCandidates}
      />
    </div>
  );
}
