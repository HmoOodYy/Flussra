import type { DayGridColumn } from '../../types/payroll/entry';
import type { RowFilter } from './dayEntryPageModel';
import type { EntryView } from './periodOverviewModel';
import styles from './PayrollEntryPage.module.css';

const FILTERS: Array<{ key: RowFilter; label: string }> = [
  { key: 'all', label: 'All drivers' },
  { key: 'attention', label: 'Needs attention' },
  { key: 'notEntered', label: 'Not entered' },
  { key: 'unsaved', label: 'Unsaved' },
];

interface EntryToolbarProps {
  query: string;
  onQuery: (query: string) => void;
  view: EntryView;
  onView: (view: EntryView) => void;
  /** Whether the whole-period read is available, which the Week view needs. */
  weekAvailable: boolean;
  filter: RowFilter;
  onFilter: (filter: RowFilter) => void;
  columns: readonly DayGridColumn[];
  showKey: string;
  onShowKey: (key: string) => void;
}

/** Search, the Day / Week switch, and the view's own controls: row filters, or the column shown. */
export function EntryToolbar({
  query, onQuery, view, onView, weekAvailable, filter, onFilter, columns, showKey, onShowKey,
}: EntryToolbarProps) {
  return (
    <div className={styles.toolbar}>
      <label className={styles.search}>
        <span className={styles.srOnly}>Find a driver</span>
        <input
          type="search"
          className={styles.searchInput}
          placeholder="Find a driver"
          value={query}
          onChange={(e) => onQuery(e.target.value)}
        />
      </label>

      {view === 'day' && (
        <div className={styles.chips} role="group" aria-label="Filter drivers">
          {FILTERS.map((option) => (
            <button
              key={option.key}
              type="button"
              className={`${styles.chip} ${filter === option.key ? styles.chipActive : ''}`}
              aria-pressed={filter === option.key}
              onClick={() => onFilter(option.key)}
            >
              {option.label}
            </button>
          ))}
        </div>
      )}

      <div className={styles.toolbarEnd}>
        {view === 'week' && (
          <label className={styles.showSelect}>
            <span>Show</span>
            <select value={showKey} onChange={(e) => onShowKey(e.target.value)}>
              {columns.map((column) => (
                <option key={column.payroll_period_definition_id} value={String(column.payroll_period_definition_id)}>
                  {column.label}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className={styles.segmented} role="group" aria-label="View">
          <button
            type="button"
            className={`${styles.segBtn} ${view === 'day' ? styles.segBtnActive : ''}`}
            aria-pressed={view === 'day'}
            onClick={() => onView('day')}
          >
            Day
          </button>
          <button
            type="button"
            className={`${styles.segBtn} ${view === 'week' ? styles.segBtnActive : ''}`}
            aria-pressed={view === 'week'}
            disabled={!weekAvailable}
            title={weekAvailable ? undefined : 'The week is not available right now.'}
            onClick={() => onView('week')}
          >
            Week
          </button>
        </div>
      </div>
    </div>
  );
}
