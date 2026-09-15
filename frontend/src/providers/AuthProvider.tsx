import { useState } from 'react';
import type { ReactNode } from 'react';
import { AuthContext } from '../store/authStore';
import type { UserProfile } from '../store/authStore';

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUserState] = useState<UserProfile | null>(null);
  const [isLoading, setLoading] = useState(true);

  function setUser(u: UserProfile | null) {
    setUserState(u);
  }

  function logout() {
    sessionStorage.removeItem('access_token');
    setUserState(null);
  }

  return (
    <AuthContext.Provider value={{
      user, isLoading, setUser, setLoading, logout,
    }}>
      {children}
    </AuthContext.Provider>
  );
}
