import { dayChip, groupIntoWeeks, weekIndexOf } from './dayEntryPageModel';
import type { DayProgress } from './periodOverviewModel';
import styles from './PayrollEntryPage.module.css';

interface DaySelectorProps {
  /** Every date of the period, in order. */
  dates: readonly string[];
  selectedDate: string | null;
  disabled: boolean;
  /** Each date's saved progress; null when the overview could not be read. */
  progress: ReadonlyMap<string, DayProgress> | null;
  onSelect: (date: string) => void;
}

/** One button per day of the period, a week at a time when the period is longer than a week. */
export function DaySelector({ dates, selectedDate, disabled, progress, onSelect }: DaySelectorProps) {
  const weeks = groupIntoWeeks(dates);
  const weekIndex = weekIndexOf(weeks, selectedDate);
  const visible = weeks[weekIndex] ?? [];
  const selectedPosition = selectedDate === null ? -1 : dates.indexOf(selectedDate);

  return (
    <section className={styles.daySelector} aria-label="Select a day">
      {weeks.length > 1 && (
        <div className={styles.weekSwitcher} role="group" aria-label="Select a week">
          {weeks.map((week, index) => (
            <button
              key={week[0]}
              type="button"
              className={`${styles.weekBtn} ${index === weekIndex ? styles.weekBtnActive : ''}`}
              aria-pressed={index === weekIndex}
              disabled={disabled}
              onClick={() => onSelect(week[0])}
            >
              Week {index + 1}
            </button>
          ))}
        </div>
      )}
      <div className={styles.dayRow}>
        <button
          type="button"
          className={styles.dayStepBtn}
          aria-label="Previous day"
          disabled={disabled || selectedPosition <= 0}
          onClick={() => onSelect(dates[selectedPosition - 1])}
        >
          &#8592;
        </button>
        <div className={styles.dayList}>
          {visible.map((date) => {
            const chip = dayChip(date);
            const selected = date === selectedDate;
            const state = progress?.get(date);
            return (
              <button
                key={date}
                type="button"
                className={`${styles.dayBtn} ${selected ? styles.dayBtnSelected : ''} ${state?.configuredOff ? styles.dayBtnOff : ''}`}
                aria-label={state ? `${chip.weekday} ${chip.dayOfMonth}, ${state.text}` : `${chip.weekday} ${chip.dayOfMonth}`}
                aria-pressed={selected}
                disabled={disabled}
                onClick={() => onSelect(date)}
              >
                <span className={styles.dayWeekday}>{chip.weekday}</span>
                <span className={styles.dayNumber}>{chip.dayOfMonth}</span>
                {state && (
                  <>
                    <span className={`${styles.dayState} ${styles[`dayState_${state.tone}`]}`}>{state.text}</span>
                    <span className={styles.dayBar} aria-hidden="true">
                      <span
                        className={`${styles.dayBarFill} ${styles[`dayBar_${state.barTone}`]}`}
                        style={{ width: `${Math.round(state.ratio * 100)}%` }}
                      />
                    </span>
                  </>
                )}
              </button>
            );
          })}
        </div>
        <button
          type="button"
          className={styles.dayStepBtn}
          aria-label="Next day"
          disabled={disabled || selectedPosition < 0 || selectedPosition >= dates.length - 1}
          onClick={() => onSelect(dates[selectedPosition + 1])}
        >
          &#8594;
        </button>
      </div>
    </section>
  );
}
