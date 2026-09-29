import React, { useEffect, useRef, useState } from 'react';
import styles from './DateInput.module.css';
import { type DateParts, isoToParts, parseDateParts } from '../../lib/isoDate';

export type DateInputStatus = 'empty' | 'incomplete' | 'invalid' | 'valid';

type DateInputProps = {
  id: string;
  label: string;
  /** ISO date (YYYY-MM-DD) or ''. */
  value: string;
  onChange: (iso: string, status: DateInputStatus) => void;
  hint?: string;
  error?: string | null;
  disabled?: boolean;
  required?: boolean;
  describedBy?: string;
};

/**
 * Generic three-field (Day / Month / Year) date entry. No browser calendar
 * popup, no auto-advance between fields (accessibility — the user drives Tab
 * order). Never rewrites what the user typed while it is invalid/incomplete;
 * see the external-sync comment below.
 */
export function DateInput({
  id,
  label,
  value,
  onChange,
  hint,
  error,
  disabled = false,
  required = false,
  describedBy,
}: DateInputProps) {
  const [parts, setParts] = useState<DateParts>(() => isoToParts(value));
  const [hasBlurredGroup, setHasBlurredGroup] = useState(false);
  // The last ISO value (possibly '') this component itself emitted via
  // onChange. Used to tell "the parent changed `value` out from under us"
  // (a stepper, a loaded suggestion) apart from "our own typing produced
  // this value" — only the former should ever overwrite typed parts.
  const lastEmittedRef = useRef<string | null>(null);
  const fieldsetRef = useRef<HTMLFieldSetElement | null>(null);

  useEffect(() => {
    if (value !== lastEmittedRef.current) {
      setParts(isoToParts(value));
    }
  }, [value]);

  function handleFieldChange(field: keyof DateParts, raw: string, maxLen: number) {
    const digits = raw.replace(/\D/g, '').slice(0, maxLen);
    const next = { ...parts, [field]: digits };
    setParts(next);
    const result = parseDateParts(next);
    const iso = result.kind === 'valid' ? result.iso : '';
    lastEmittedRef.current = iso;
    onChange(iso, result.kind);
  }

  function handleGroupBlur(event: React.FocusEvent<HTMLFieldSetElement>) {
    const next = event.relatedTarget as Node | null;
    if (!next || !fieldsetRef.current?.contains(next)) {
      setHasBlurredGroup(true);
    }
  }

  const parseResult = parseDateParts(parts);
  const allFilled = parts.day !== '' && parts.month !== '' && parts.year !== '';
  const internalMessage =
    parseResult.kind === 'invalid' && (hasBlurredGroup || allFilled) ? parseResult.message : null;
  const displayedMessage = error != null && error !== '' ? error : internalMessage;
  const hintId = hint ? `${id}-hint` : null;
  const messageId = displayedMessage ? `${id}-message` : null;
  const describedByIds = [hintId, messageId, describedBy].filter(Boolean).join(' ') || undefined;

  return (
    <fieldset
      ref={fieldsetRef}
      id={id}
      className={styles.root}
      disabled={disabled}
      onBlur={handleGroupBlur}
    >
      <legend className={styles.legend}>
        {label}
        {required && (
          <span className={styles.required} aria-hidden="true">
            {' '}
            *
          </span>
        )}
      </legend>
      <div className={styles.fieldsRow}>
        <div className={styles.field}>
          <label htmlFor={`${id}-day`} className={styles.fieldLabel}>
            Day
          </label>
          <input
            id={`${id}-day`}
            className={[styles.input, styles.inputDay].join(' ')}
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            autoComplete="off"
            maxLength={2}
            value={parts.day}
            onChange={(e) => handleFieldChange('day', e.target.value, 2)}
            disabled={disabled}
            aria-invalid={displayedMessage != null}
            aria-describedby={describedByIds}
          />
        </div>
        <div className={styles.field}>
          <label htmlFor={`${id}-month`} className={styles.fieldLabel}>
            Month
          </label>
          <input
            id={`${id}-month`}
            className={[styles.input, styles.inputMonth].join(' ')}
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            autoComplete="off"
            maxLength={2}
            value={parts.month}
            onChange={(e) => handleFieldChange('month', e.target.value, 2)}
            disabled={disabled}
            aria-invalid={displayedMessage != null}
            aria-describedby={describedByIds}
          />
        </div>
        <div className={styles.field}>
          <label htmlFor={`${id}-year`} className={styles.fieldLabel}>
            Year
          </label>
          <input
            id={`${id}-year`}
            className={[styles.input, styles.inputYear].join(' ')}
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            autoComplete="off"
            maxLength={4}
            value={parts.year}
            onChange={(e) => handleFieldChange('year', e.target.value, 4)}
            disabled={disabled}
            aria-invalid={displayedMessage != null}
            aria-describedby={describedByIds}
          />
        </div>
      </div>
      {hint && (
        <p id={hintId ?? undefined} className={styles.hint}>
          {hint}
        </p>
      )}
      {displayedMessage && (
        <p id={messageId ?? undefined} role="alert" aria-live="polite" className={styles.message}>
          {displayedMessage}
        </p>
      )}
    </fieldset>
  );
}
