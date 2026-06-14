import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import apiClient from '../lib/apiClient';
import { useAuth, toUserProfile } from '../store/authStore';
import type { UserInfoResponse } from '../store/authStore';

export function AuthBootstrap({ children }: { children: React.ReactNode }) {
  const { setUser, setLoading, isLoading } = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    const token = sessionStorage.getItem('access_token');
    if (!token) {
      setLoading(false);
      navigate('/login', { replace: true });
      return;
    }

    apiClient
      .get<UserInfoResponse>('/auth/me')
      .then((res) => {
        setUser(toUserProfile(res.data));
        setLoading(false);
      })
      .catch(() => {
        sessionStorage.removeItem('access_token');
        setUser(null);
        setLoading(false);
        navigate('/login', { replace: true });
      });
    // run once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (isLoading) {
    return (
      <div style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center' }}>
        <p style={{ color: '#6b7280', fontSize: '1rem' }}>Loading…</p>
      </div>
    );
  }

  return <>{children}</>;
}
