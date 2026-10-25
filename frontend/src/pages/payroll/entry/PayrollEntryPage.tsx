/**
 * PayrollEntryPage — a payroll period's entry page: the Day view (one day of the daily entry
 * grid at a time, editable) and the Week view (every driver across a week, read only).
 *
 * The one operational Day Grid surface, reached from the period card on Current Payroll.
 * It fetches its own data via getDayGrid and the period's saved entries via the entry overview;
 * columns are the period's frozen definitions and every amount shown is the backend's.
 */
import { useEffect, useState, type MouseEvent, type ReactNode } from 'react';
import { useParams } from 'react-router-dom';
import { formatMoney } from '../../../lib/money';
import { canViewPayRates } from '../../../lib/permissions';
import { useAuth } from '../../../store/authStore';
import { daySaveErrorMessage, quantityTotalViews } from './dayGridModel';
import {
  dayPayByDriver, dayPayNote, filterEntryRows, formatQuantity, groupIntoWeeks, listPeriodDates,
  matchesDriverSearch, parsePeriodId, sumDecimalStrings, unsavedChangesLabel, weekIndexOf, type RowFilter,
} from './dayEntryPageModel';
import {
  dayProgress, daysOfWeek, daysWorked, driversOfDays, driverWeekBars, weekPayByDriver, type EntryView,
} from './periodOverviewModel';
import { DayEntryTable } from './DayEntryTable';
import { DaySelector } from './DaySelector';
import { DriverPanel } from './DriverPanel';
import { EntryToolbar } from './EntryToolbar';
import { BackToCurrentPayroll, PeriodEntryHeader, PeriodStatusBanner } from './PeriodEntryHeader';
import { WeekTable } from './WeekTable';
import { formatDayLabel, useDailyEntryController } from './useDailyEntryController';
import { usePeriodOverview } from './usePeriodOverview';
import { usePeriodPreview } from './usePeriodPreview';
import styles from './PayrollEntryPage.module.css';

const DISCARD_PROMPT = 'Discard unsaved changes?';
const SAVED_NOTICE_MS = 4000;

export function PayrollEntryPage() {
  const { periodId: rawPeriodId } = useParams();
  const periodId = parsePeriodId(rawPeriodId);

  if (periodId === null) {
    return (
      <div className={styles.page}>
        <BackToCurrentPayroll />
        <div className={styles.errorCard} role="alert">
          This payroll period link is not valid. Open the period from Current payroll.
        </div>
      </div>
    );
  }
  return <PeriodEntry key={periodId} periodId={periodId} />;
}

function SummaryCard({ label, value, unit, sub, attention }: {
  label: string;
  value: string | number;
  /** The company-defined unit of a quantity, shown as written beside the number. */
  unit?: string | null;
  sub?: string;
  attention?: boolean;
}) {
  return (
    <div className={`${styles.card} ${attention ? styles.cardAttention : ''}`}>
      <span className={styles.cardLabel}>{label}</span>
      <span className={styles.cardValue}>
        {value}
        {unit && <span className={styles.cardUnit}>{unit}</span>}
      </span>
      {sub && <span className={styles.cardSub}>{sub}</span>}
    </div>
  );
}

function PeriodEntry({ periodId }: { periodId: number }) {
  const { user } = useAuth();

  const {
    selectedDate, grid, loading, error, dirtyRows, saving, saveError,
    fetchGrid, selectDate, discardChanges, isEditable, canEdit, handleSave,
    handleCellChange, handleStatusChange, handleNotesChange,
    getCellValue, getStatusText, getNotesValue,
  } = useDailyEntryController({ periodId, user, formatSaveError: daySaveErrorMessage });
  const { preview, previewFailed, previewLoading, refresh: refreshPreview } = usePeriodPreview(periodId, grid?.period.status ?? null);
  const { overview, refresh: refreshOverview } = usePeriodOverview(periodId);

  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<RowFilter>('all');
  const [savedNotice, setSavedNotice] = useState(false);
  const [requestedView, setRequestedView] = useState<EntryView>('day');
  const [showKeyChoice, setShowKeyChoice] = useState<string | null>(null);
  const [panelDriverId, setPanelDriverId] = useState<number | null>(null);

  const dirtyCount = dirtyRows.size;
  // The Week view reads the overview; without it the page stays on the Day view.
  const view: EntryView = requestedView === 'week' && overview ? 'week' : 'day';

  useEffect(() => {
    // The day a read returned is already shown; only a day picked since needs its own read.
    if (grid?.work_date === selectedDate) return;
    void fetchGrid(selectedDate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDate, periodId]);

  // ── Unsaved-changes protection ───────────────────────────────────────────── //
  useEffect(() => {
    if (dirtyCount === 0) return;
    function warn(event: BeforeUnloadEvent) {
      event.preventDefault();
      event.returnValue = '';
    }
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirtyCount]);

  useEffect(() => {
    if (!savedNotice) return;
    const timer = window.setTimeout(() => setSavedNotice(false), SAVED_NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [savedNotice]);

  function confirmDiscard(): boolean {
    return dirtyCount === 0 || window.confirm(DISCARD_PROMPT);
  }

  function handleBack(event: MouseEvent<HTMLAnchorElement>) {
    if (!confirmDiscard()) event.preventDefault();
  }

  /** Select a day; false when the user kept their unsaved changes instead. */
  function goToDay(date: string): boolean {
    if (date === selectedDate) return true;
    if (!confirmDiscard()) return false;
    setSavedNotice(false);
    selectDate(date);
    return true;
  }

  function handleView(next: EntryView) {
    if (next === requestedView) return;
    // The Week view shows saved entries only, so unsaved edits are dropped when leaving the Day view.
    if (next === 'week') {
      if (!confirmDiscard()) return;
      discardChanges();
    }
    setRequestedView(next);
  }

  function handleWeekDayClick(date: string) {
    if (goToDay(date)) setRequestedView('day');
  }

  async function handleSaveClick() {
    if (await handleSave()) {
      setSavedNotice(true);
      refreshPreview();
      refreshOverview();
    }
  }

  // ── Derived view ─────────────────────────────────────────────────────────── //
  const period = grid?.period ?? null;
  const dates = period ? listPeriodDates(period.start_date, period.end_date) : [];
  const weeks = groupIntoWeeks(dates);
  const weekDates = weeks[weekIndexOf(weeks, selectedDate)] ?? [];
  const overviewDays = overview?.days ?? [];
  const weekDays = daysOfWeek(overviewDays, weekDates);
  const progress = overview ? new Map(overviewDays.map((day) => [day.work_date, dayProgress(day)])) : null;

  const columns = overview?.columns ?? grid?.columns ?? [];
  const shownColumn = columns.find((col) => String(col.payroll_period_definition_id) === showKeyChoice) ?? columns[0];
  const shownKey = shownColumn ? String(shownColumn.payroll_period_definition_id) : '';

  const formatPreviewMoney = (amount: string) =>
    formatMoney(amount, preview?.currency_code, preview?.currency_minor_unit_digits);

  function renderPanel(): ReactNode {
    if (panelDriverId === null || !period) return null;
    const driver = grid?.rows.find((row) => row.driver_assignment_id === panelDriverId)
      ?? driversOfDays(overviewDays).find((d) => d.driver_assignment_id === panelDriverId);
    if (!driver) return null;
    return (
      <DriverPanel
        name={driver.driver_name}
        number={driver.driver_number}
        branchName={period.branch_name}
        columns={columns}
        showKey={shownKey}
        onShowKey={setShowKeyChoice}
        bars={overview ? driverWeekBars(weekDays, panelDriverId, shownKey) : null}
        daysWorked={overview ? daysWorked(weekDays, panelDriverId) : null}
        preview={preview}
        previewDriver={preview?.drivers.find((d) => d.driver_assignment_id === panelDriverId)}
        canFixRates={user !== null && canViewPayRates(user)}
        onClose={() => setPanelDriverId(null)}
      />
    );
  }

  /** The table area beside the driver panel, which shrinks it rather than covering it. */
  function withPanel(main: ReactNode) {
    return (
      <div className={styles.workspace}>
        <div className={styles.workspaceMain}>{main}</div>
        {renderPanel()}
      </div>
    );
  }

  function renderToolbar() {
    return (
      <EntryToolbar
        query={query}
        onQuery={setQuery}
        view={view}
        onView={handleView}
        weekAvailable={overview !== null}
        filter={filter}
        onFilter={setFilter}
        columns={columns}
        showKey={shownKey}
        onShowKey={setShowKeyChoice}
      />
    );
  }

  function renderWeek() {
    if (!shownColumn || weekDates.length === 0) return null;
    const drivers = driversOfDays(weekDays).filter((driver) => matchesDriverSearch(driver, query));
    const weekPay = preview ? weekPayByDriver(preview.drivers, weekDates) : null;
    return (
      <>
        {renderToolbar()}
        {withPanel(
          <div className={`${styles.tableCard} ${styles.tableCardReadOnly}`}>
            <WeekTable
              weekDates={weekDates}
              days={weekDays}
              drivers={drivers}
              column={shownColumn}
              weekPay={weekPay}
              formatPay={formatPreviewMoney}
              panelDriverId={panelDriverId}
              onSelectDay={handleWeekDayClick}
              onOpenDriver={setPanelDriverId}
            />
          </div>,
        )}
      </>
    );
  }

  function renderBody() {
    if (view === 'week') return renderWeek();
    if (loading) return <div className={styles.stateCard} role="status">Loading…</div>;
    if (error) {
      return (
        <div className={styles.errorCard} role="alert">
          <span>{error}</span>
          <button type="button" className={styles.secondaryBtn} onClick={() => void fetchGrid(selectedDate)}>
            Try again
          </button>
        </div>
      );
    }
    if (!grid) return null;

    const dayPay = preview ? dayPayByDriver(preview.drivers, grid.work_date) : null;
    const dayPayTotal = dayPay
      ? sumDecimalStrings(grid.rows.map((row) => dayPay.get(row.driver_assignment_id)))
      : null;
    const dayPayTotalText = dayPayTotal === null ? '—' : formatPreviewMoney(dayPayTotal);
    const configuredOff = overviewDays.find((day) => day.work_date === grid.work_date)?.is_configured_off_day ?? false;

    const states = grid.rows.map((row) => ({
      row,
      dirty: dirtyRows.has(row.driver_assignment_id),
      statusText: getStatusText(row),
      values: Object.fromEntries(
        grid.columns.map((col) => {
          const key = String(col.payroll_period_definition_id);
          return [key, getCellValue(row, key)];
        }),
      ),
    }));
    const visibleRows = filterEntryRows(states, grid.columns, filter, query).map((state) => state.row);

    return (
      <>
        <section className={styles.cards} aria-label={`Summary for ${formatDayLabel(grid.work_date)}`}>
          <SummaryCard label="Drivers" value={grid.summary.total_drivers} sub="in this payroll" />
          <SummaryCard label="Working" value={grid.summary.worked} sub="not on a full day off" />
          <SummaryCard label="Day off" value={grid.summary.off} sub="status counts as a full day off" />
          {quantityTotalViews(grid.columns, grid.summary).map((total) => (
            <SummaryCard
              key={total.key}
              label={total.label}
              value={formatQuantity(total.quantity)}
              unit={total.unit}
            />
          ))}
          <SummaryCard
            label="Day pay"
            value={dayPayTotalText}
            sub={dayPayNote({
              periodStatus: grid.period.status,
              loading: previewLoading,
              failed: previewFailed,
              hasAmount: dayPayTotal !== null,
            })}
          />
          <SummaryCard
            label="Period pay so far"
            value={preview ? formatMoney(preview.total_expected_pay, preview.currency_code, preview.currency_minor_unit_digits) : '—'}
            sub="before review"
          />
          <SummaryCard
            label="Needs attention"
            value={grid.summary.needs_attention}
            sub={grid.summary.needs_attention > 0 ? 'cells marked in orange' : 'nothing to check'}
            attention={grid.summary.needs_attention > 0}
          />
        </section>

        {renderToolbar()}

        {configuredOff && <p className={styles.offNote}>Normal day off — entries are still allowed.</p>}
        <p className={styles.hint}>Tab moves to the next cell · Shift+Tab moves back</p>

        {withPanel(
          <div className={styles.tableCard}>
            <DayEntryTable
              grid={grid}
              rows={visibleRows}
              dirtyRows={dirtyRows}
              canEdit={canEdit}
              panelDriverId={panelDriverId}
              onOpenDriver={setPanelDriverId}
              dayPay={dayPay}
              formatDayPay={formatPreviewMoney}
              dayPayTotalText={dayPayTotalText}
              getCellValue={getCellValue}
              getStatusText={getStatusText}
              getNotesValue={getNotesValue}
              onCellChange={handleCellChange}
              onStatusChange={handleStatusChange}
              onNotesChange={handleNotesChange}
            />
            {canEdit && dirtyCount > 0 && (
              <div className={styles.saveBar}>
                <span className={styles.saveText}>{unsavedChangesLabel(dirtyCount)}</span>
                {saveError && <span className={styles.saveError} role="alert">{saveError}</span>}
                <div className={styles.saveActions}>
                  <button type="button" className={styles.secondaryBtn} disabled={saving} onClick={discardChanges}>
                    Discard
                  </button>
                  <button type="button" className={styles.primaryBtn} disabled={saving} onClick={() => void handleSaveClick()}>
                    {saving ? 'Saving…' : 'Save changes'}
                  </button>
                </div>
              </div>
            )}
            {savedNotice && dirtyCount === 0 && (
              <div className={styles.savedBar} role="status">All changes saved</div>
            )}
          </div>,
        )}
      </>
    );
  }

  return (
    <div className={styles.page}>
      {period ? (
        <PeriodEntryHeader period={period} onBack={handleBack} />
      ) : (
        <header className={styles.header}>
          <BackToCurrentPayroll />
          <h1 className={styles.title}>{loading ? 'Loading…' : 'Payroll period'}</h1>
        </header>
      )}

      {period && <PeriodStatusBanner status={period.status} isEditable={isEditable} />}

      {period && (
        <DaySelector
          dates={dates}
          selectedDate={selectedDate}
          disabled={saving}
          progress={progress}
          onSelect={goToDay}
        />
      )}

      {renderBody()}
    </div>
  );
}
