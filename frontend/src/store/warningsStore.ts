import { createContext, useContext } from 'react';
import type { SetupWarning } from '../types/dashboard';

export interface WarningsContextValue {
  warnings: SetupWarning[];
  setWarnings: (w: SetupWarning[]) => void;
}

export const WarningsContext = createContext<WarningsContextValue>({
  warnings: [],
  setWarnings: () => {},
});

export function useWarningsStore<T>(selector: (v: WarningsContextValue) => T): T {
  return selector(useContext(WarningsContext));
}
