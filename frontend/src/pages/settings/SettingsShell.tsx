/**
 * SettingsShell — wrapper for the Settings section.
 *
 * Navigation is now handled entirely by the AppShell sidebar.
 * This shell simply provides the height-filling flex container
 * and renders the active settings page via <Outlet />.
 */
import { Outlet } from 'react-router-dom';
import styles from './SettingsShell.module.css';

export function SettingsShell() {
  return (
    <div className={styles.page}>
      <div className={styles.content}>
        <Outlet />
      </div>
    </div>
  );
}
