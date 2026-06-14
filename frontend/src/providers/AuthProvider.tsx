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

  function hasPermission(code: string): boolean {
    return user?.active_permissions.includes(code) ?? false;
  }

  function hasAnyPermission(codes: string[]): boolean {
    if (!user) return false;
    return codes.some((c) => user.active_permissions.includes(c));
  }

  function hasAllPermissions(codes: string[]): boolean {
    if (!user) return false;
    return codes.every((c) => user.active_permissions.includes(c));
  }

  return (
    <AuthContext.Provider value={{
      user, isLoading, setUser, setLoading, logout,
      hasPermission, hasAnyPermission, hasAllPermissions,
    }}>
      {children}
    </AuthContext.Provider>
  );
}
