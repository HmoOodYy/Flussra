/** Polished placeholder for sections not yet built. */
export function PlaceholderPage({
  title,
  subtitle,
  note,
}: {
  title: string;
  subtitle: string;
  note?: string;
}) {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      height: '100%',
      minHeight: '360px',
      padding: '2rem',
    }}>
      <div style={{
        background: '#fff',
        border: '1px solid #e5e7eb',
        borderRadius: '14px',
        padding: '2.5rem 2rem',
        maxWidth: '420px',
        width: '100%',
        textAlign: 'center',
        boxShadow: '0 1px 6px rgba(0,0,0,0.06)',
      }}>
        {/* Icon */}
        <div style={{
          width: '52px', height: '52px', borderRadius: '12px',
          background: '#eff6ff', display: 'flex', alignItems: 'center',
          justifyContent: 'center', margin: '0 auto 1.25rem',
        }}>
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
            stroke="#3b82f6" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
        </div>
        <h2 style={{
          margin: '0 0 0.45rem',
          fontSize: '1.1rem',
          fontWeight: 700,
          color: '#111827',
          letterSpacing: '-0.01em',
        }}>{title}</h2>
        <p style={{
          margin: '0 0 0.9rem',
          fontSize: '0.875rem',
          color: '#6b7280',
          lineHeight: 1.55,
        }}>{subtitle}</p>
        {note && (
          <p style={{
            margin: 0,
            fontSize: '0.8rem',
            color: '#94a3b8',
            fontStyle: 'italic',
          }}>{note}</p>
        )}
      </div>
    </div>
  );
}
