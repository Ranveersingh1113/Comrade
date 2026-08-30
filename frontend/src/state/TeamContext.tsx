import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { supabase } from '../lib/supabase';
import type { Membership, Profile, Team } from '../lib/types';
import { useAuth } from './AuthContext';

export interface RosterMember {
  membership: Membership;
  profile: Profile;
}

interface TeamState {
  team: Team | null;
  roster: RosterMember[];
  myUserId: string;
  isLeader: boolean;
  loading: boolean;
  profileOf: (userId: string | null) => Profile | null;
  refreshRoster: () => Promise<void>;
}

const TeamContext = createContext<TeamState | null>(null);

export function TeamProvider({ teamId, children }: { teamId: string; children: ReactNode }) {
  const { session } = useAuth();
  const myUserId = session?.user.id ?? '';
  const [team, setTeam] = useState<Team | null>(null);
  const [roster, setRoster] = useState<RosterMember[]>([]);
  const [loading, setLoading] = useState(true);

  const loadRoster = async () => {
    const [{ data: teamRow }, { data: members }] = await Promise.all([
      supabase.from('teams').select('*').eq('id', teamId).maybeSingle(),
      supabase.from('memberships').select('*').eq('team_id', teamId).eq('status', 'active'),
    ]);
    setTeam((teamRow as Team | null) ?? null);
    const memberships = (members as Membership[] | null) ?? [];
    if (memberships.length > 0) {
      const { data: profiles } = await supabase
        .from('profiles')
        .select('*')
        .in(
          'id',
          memberships.map((m) => m.user_id),
        );
      const byId = new Map(((profiles as Profile[] | null) ?? []).map((p) => [p.id, p]));
      setRoster(
        memberships
          .filter((m) => byId.has(m.user_id))
          .map((m) => ({ membership: m, profile: byId.get(m.user_id)! }))
          .sort((a, b) => a.profile.display_name.localeCompare(b.profile.display_name)),
      );
    } else {
      setRoster([]);
    }
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadRoster().finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [teamId]); // eslint-disable-line react-hooks/exhaustive-deps

  const value = useMemo<TeamState>(() => {
    const profileMap = new Map(roster.map((r) => [r.profile.id, r.profile]));
    return {
      team,
      roster,
      myUserId,
      isLeader: roster.some(
        (r) => r.membership.user_id === myUserId && r.membership.role === 'leader',
      ),
      loading,
      profileOf: (userId) => (userId ? (profileMap.get(userId) ?? null) : null),
      refreshRoster: loadRoster,
    };
  }, [team, roster, myUserId, loading]); // eslint-disable-line react-hooks/exhaustive-deps

  return <TeamContext.Provider value={value}>{children}</TeamContext.Provider>;
}

export function useTeam(): TeamState {
  const ctx = useContext(TeamContext);
  if (!ctx) throw new Error('useTeam outside TeamProvider');
  return ctx;
}
