import { useAuth } from '../store/authStore';

export function HomePage() {
  const { user } = useAuth();

  return (
    <div>
      <h2 style={{ margin: '0 0 0.5rem', fontSize: '1.25rem', fontWeight: 700, color: '#111827' }}>
        Welcome, {user?.display_name}
      </h2>
      <p style={{ color: '#6b7280', fontSize: '0.9rem' }}>
        You are signed in to <strong>{user?.company_name}</strong>.
        The dashboard and payroll screens will be built in the next phase.
      </p>
    </div>
  );
}
