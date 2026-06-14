import { useState } from 'react';
import type { ReactNode } from 'react';
import { WarningsContext } from '../store/warningsStore';
import type { SetupWarning } from '../types/dashboard';

export function WarningsProvider({ children }: { children: ReactNode }) {
  const [warnings, setWarnings] = useState<SetupWarning[]>([]);
  return (
    <WarningsContext.Provider value={{ warnings, setWarnings }}>
      {children}
    </WarningsContext.Provider>
  );
}
