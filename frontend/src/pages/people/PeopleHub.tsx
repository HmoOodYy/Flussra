import { NavLink, Navigate, Outlet } from 'react-router-dom';
import { useAuth } from '../../store/authStore';
import { defaultPeoplePath, peopleCapabilities, visiblePeopleChildren } from './peopleHubModel';
import styles from './PeoplePage.module.css';

/**
 * The People & Access hub. It groups three independently routed and independently authorized
 * areas (Workforce, Access, Transfer Requests) under one entry; it holds no data of its own and
 * grants nothing. Each child route keeps its own permission gate.
 */
export function PeopleHub() {
  const { user } = useAuth();
  if (!user) return null;
  const children = visiblePeopleChildren(peopleCapabilities(user));

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>People &amp; Access</h1>
          <p className={styles.sub}>Workforce records, account access and driver transfers.</p>
        </div>
      </div>

      <nav className={styles.tabBar} aria-label="People areas">
        {children.map((child) => (
          <NavLink
            key={child.key}
            to={child.path}
            className={({ isActive }) => `${styles.tabBtn}${isActive ? ` ${styles.tabBtnActive}` : ''}`}
          >
            {child.label}
          </NavLink>
        ))}
      </nav>

      <Outlet />
    </div>
  );
}

/** `/people` itself: send the user to the first area they may enter (Workforce, Access, Transfers). */
export function PeopleDefaultRedirect() {
  const { user } = useAuth();
  const target = user ? defaultPeoplePath(peopleCapabilities(user)) : null;
  return <Navigate to={target ?? '/dashboard'} replace />;
}
