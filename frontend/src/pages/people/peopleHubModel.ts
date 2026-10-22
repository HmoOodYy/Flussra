/**
 * Pure model of the People & Access hub: which of its three areas a user can enter.
 *
 * The hub is a presentation grouping, not an authority. Workforce, Access and Transfer Requests
 * keep separate backend authorities and separate frontend gates; this module only combines their
 * results to build the tab bar and the `/people` default redirect. No React, no I/O.
 */
import type { UserProfile } from '../../store/authStore.ts';
import { canViewAccess, canViewTransfers, canViewWorkforce } from '../../lib/permissions.ts';

export type PeopleChild = 'workforce' | 'access' | 'transfers';

export interface PeopleChildInfo {
  key: PeopleChild;
  label: string;
  path: string;
}

/** The hub's areas in redirect and display order. */
export const PEOPLE_CHILDREN: readonly PeopleChildInfo[] = [
  { key: 'workforce', label: 'Workforce', path: '/people/workforce' },
  { key: 'access', label: 'Access', path: '/people/access' },
  { key: 'transfers', label: 'Transfer Requests', path: '/people/transfers' },
];

export type PeopleCapabilities = Record<PeopleChild, boolean>;

export function peopleCapabilities(user: UserProfile): PeopleCapabilities {
  return {
    workforce: canViewWorkforce(user),
    access: canViewAccess(user),
    transfers: canViewTransfers(user),
  };
}

/** The areas the user may enter, in display order. */
export function visiblePeopleChildren(caps: PeopleCapabilities): PeopleChildInfo[] {
  return PEOPLE_CHILDREN.filter((child) => caps[child.key]);
}

/** Where `/people` sends the user: the first authorized area (Workforce, Access, Transfers), else nowhere. */
export function defaultPeoplePath(caps: PeopleCapabilities): string | null {
  return visiblePeopleChildren(caps)[0]?.path ?? null;
}

export function peopleChildForPath(pathname: string): PeopleChild | null {
  const match = PEOPLE_CHILDREN.find(
    (child) => pathname === child.path || pathname.startsWith(`${child.path}/`));
  return match?.key ?? null;
}
