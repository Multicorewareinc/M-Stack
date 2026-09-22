import {
  Badge,
  DataTable,
  EmptyState,
  PageHeader,
  Select,
  StatCard,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  type Column,
} from "@multistack/ui";
import * as React from "react";
import type { ApiKey, ApiKeyStatus } from "../../api/apiKeys";
import type { User } from "../../api/types";
import type { UsageByKeyRow, UsageByUserRow } from "../../api/usage";
import { useAuth } from "../../auth/AuthContext";
import { shouldUseRealAuth } from "../../auth/env";
import { formatDate, formatNumber, formatQuota } from "../../lib/formatters";
import { useApiKeys } from "../apiKeys/hooks/useApiKeys";
import { useOrgSummary } from "../settings/hooks/useOrgSummary";
import { useUsers } from "../users/hooks/useUsers";
import { useUsageByKey } from "./hooks/useUsageByKey";
import { useUsageByUser } from "./hooks/useUsageByUser";
import { useUsageSummary } from "./hooks/useUsageSummary";
import { useUsageTimeseries } from "./hooks/useUsageTimeseries";

type ViewAs = "admin" | "member";

const KEY_STATUS_VARIANT: Record<
  ApiKeyStatus,
  "success" | "warning" | "neutral"
> = {
  active: "success",
  expired: "warning",
  revoked: "neutral",
};

/**
 * The org's real plan limits (organization-control-plane's /v1/organization/summary). Three
 * independent ceilings in different units (tokens/min, requests/min, tokens/month) with no
 * usage-vs-limit ratio to plot — a bar/pie/meter would imply a proportional relationship that
 * isn't there, so per the "handful of headline numbers -> KPI row of stat tiles" rule this is a
 * stat-tile row, not a fabricated chart.
 */
function PlanLimitsRow() {
  const { data: summary, isLoading, error } = useOrgSummary();

  if (error) {
    return (
      <div className="rounded-lg border border-subtle bg-neutral-0">
        <EmptyState
          title="Unable to load plan limits"
          description={error.message}
        />
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      <StatCard
        label="Monthly token quota"
        value={
          isLoading ? "—" : formatQuota(summary?.plan.quota_monthly_tokens ?? 0)
        }
        hint="org-wide, per calendar month"
      />
      <StatCard
        label="TPM limit"
        value={isLoading ? "—" : formatQuota(summary?.plan.tpm ?? 0)}
        hint="tokens per minute"
      />
      <StatCard
        label="RPM limit"
        value={isLoading ? "—" : formatQuota(summary?.plan.rpm ?? 0)}
        hint="requests per minute"
      />
    </div>
  );
}

/** Real, org-wide (organization-control-plane's /v1/usage/summary). No per-user/per-API-key
 * breakdown exists, so this is the same figure for every "View as" state — and no cost card: no
 * pricing model exists anywhere in the backend, so there is nothing real to show for it. */
function UsageKpiRow() {
  const { data, isLoading, error, refetch } = useUsageSummary();

  if (error) {
    return (
      <div className="rounded-lg border border-subtle bg-neutral-0">
        <EmptyState
          title="Unable to load usage"
          description={error.message}
          action={{ label: "Retry", onClick: () => refetch() }}
        />
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      <StatCard
        label="Requests"
        value={isLoading ? "—" : formatNumber(data?.requests ?? 0)}
        hint="current period"
      />
      <StatCard
        label="Total tokens"
        value={isLoading ? "—" : formatNumber(data?.total_tokens ?? 0)}
        hint="prompt + completion"
      />
      <StatCard
        label="Prompt / completion"
        value={
          isLoading
            ? "—"
            : `${formatNumber(data?.prompt_tokens ?? 0)} / ${formatNumber(data?.completion_tokens ?? 0)}`
        }
      />
    </div>
  );
}

/** Simple token-styled bar chart. No chart lib in this app; bars are plain divs. */
function UsageChart({ data }: { data: { date: string; tokens: number }[] }) {
  const max = Math.max(...data.map((d) => d.tokens), 1);
  return (
    <div className="rounded-lg border border-subtle bg-neutral-0 p-4">
      <div className="flex items-baseline justify-between">
        <h3 className="text-cardTitle font-semibold text-neutral-900">
          Tokens over time
        </h3>
        <span className="text-caption text-neutral-400">
          org-wide, current period
        </span>
      </div>
      <div
        className="mt-4 flex h-40 items-end gap-1.5"
        role="img"
        aria-label="Token usage per day"
      >
        {data.map((d) => (
          <div
            key={d.date}
            className="group flex h-full flex-1 flex-col items-center justify-end gap-1"
          >
            <div
              className="w-full rounded-t bg-primary-500 transition-colors group-hover:bg-primary-600"
              style={{ height: `${Math.max((d.tokens / max) * 100, 3)}%` }}
              title={`${d.date}: ${formatNumber(d.tokens)} tokens`}
            />
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-caption text-neutral-400">
        <span>{data[0]?.date}</span>
        <span>{data[data.length - 1]?.date}</span>
      </div>
    </div>
  );
}

/** Real, org-wide (organization-control-plane's /v1/usage/timeseries). Zero-filled by the
 * backend — every day in the period has a bucket — so no empty-array edge case beyond
 * loading/error. */
function UsageOverTimeChart() {
  const { data, isLoading, error, refetch } = useUsageTimeseries();

  if (error) {
    return (
      <div className="rounded-lg border border-subtle bg-neutral-0">
        <EmptyState
          title="Unable to load usage trend"
          description={error.message}
          action={{ label: "Retry", onClick: () => refetch() }}
        />
      </div>
    );
  }

  if (isLoading || !data) {
    return (
      <div className="h-[212px] animate-pulse rounded-lg border border-subtle bg-neutral-100" />
    );
  }

  return (
    <UsageChart
      data={data.buckets.map((b) => ({ date: b.date, tokens: b.total_tokens }))}
    />
  );
}

const ZERO_USAGE = { requests: 0, total_tokens: 0 };

interface UserRow {
  id: string;
  name: string;
  email: string;
  keyCount: number;
  requests: number;
  totalTokens: number;
}

interface KeyRow extends ApiKey {
  ownerName: string;
  requests: number;
  totalTokens: number;
}

/** Real ownership/identity (organization-control-plane's API-key list and user directory) merged
 * with real per-user/per-API-key request and token counts (add-per-user-key-usage-attribution).
 * `unattributedRequests` surfaces the one null-keyed bucket each endpoint can return (usage the
 * enricher couldn't attribute, e.g. events that predate this pipeline) as a footnote rather than
 * a fabricated table row, since it belongs to no real user/key. */
function UsageBreakdown({
  keys,
  users,
  usersByUser,
  usageByKey,
  unattributedUserRequests,
  unattributedKeyRequests,
  isLoading,
  error,
  onRetry,
}: {
  keys: ApiKey[];
  users: User[];
  usersByUser: Map<string, UsageByUserRow>;
  usageByKey: Map<string, UsageByKeyRow>;
  unattributedUserRequests: number;
  unattributedKeyRequests: number;
  isLoading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const nameById = new Map(users.map((u) => [u.id, u.display_name ?? u.username]));

  const userRows: UserRow[] = users
    .map((u) => {
      const usage = usersByUser.get(u.id) ?? ZERO_USAGE;
      return {
        id: u.id,
        name: u.display_name ?? u.username,
        email: u.email,
        keyCount: keys.filter((k) => k.owner_id === u.id).length,
        requests: usage.requests,
        totalTokens: usage.total_tokens,
      };
    })
    .filter((u) => u.keyCount > 0 || u.requests > 0)
    .sort((a, b) => b.totalTokens - a.totalTokens);

  const keyRows: KeyRow[] = keys.map((k) => {
    const usage = usageByKey.get(k.id) ?? ZERO_USAGE;
    return {
      ...k,
      ownerName: nameById.get(k.owner_id) ?? "Unknown",
      requests: usage.requests,
      totalTokens: usage.total_tokens,
    };
  });

  const userColumns: Column<UserRow>[] = [
    { key: "name", header: "User", accessor: (u) => u.name },
    { key: "email", header: "Email", accessor: (u) => u.email },
    { key: "keys", header: "Keys", accessor: (u) => String(u.keyCount) },
    { key: "requests", header: "Requests", accessor: (u) => formatNumber(u.requests) },
    { key: "tokens", header: "Total tokens", accessor: (u) => formatNumber(u.totalTokens) },
  ];

  const keyColumns: Column<KeyRow>[] = [
    {
      key: "name",
      header: "Key",
      render: (k) => <span className="font-mono">{k.name}</span>,
    },
    {
      key: "prefix",
      header: "Prefix",
      render: (k) => <span className="font-mono">{k.prefix}••••••••</span>,
    },
    { key: "owner", header: "Owner", accessor: (k) => k.ownerName },
    {
      key: "status",
      header: "Status",
      render: (k) => (
        <Badge variant={KEY_STATUS_VARIANT[k.status]}>
          {k.status[0].toUpperCase() + k.status.slice(1)}
        </Badge>
      ),
    },
    { key: "requests", header: "Requests", accessor: (k) => formatNumber(k.requests) },
    { key: "tokens", header: "Total tokens", accessor: (k) => formatNumber(k.totalTokens) },
    {
      key: "created",
      header: "Created",
      accessor: (k) => formatDate(k.created_at),
    },
    {
      key: "expires",
      header: "Expires",
      accessor: (k) => (k.expires_at ? formatDate(k.expires_at) : "—"),
    },
  ];

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-sectionTitle font-semibold text-neutral-900">
        Usage breakdown
      </h2>
      <Tabs defaultValue="user">
        <TabsList>
          <TabsTrigger value="user">Per user</TabsTrigger>
          <TabsTrigger value="key">Per API key</TabsTrigger>
        </TabsList>
        <TabsContent value="user">
          <DataTable
            columns={userColumns}
            rows={userRows}
            getRowId={(u) => u.id}
            loading={isLoading}
            error={error}
            onRetry={onRetry}
          />
          {!isLoading && !error && unattributedUserRequests > 0 && (
            <p className="mt-2 text-caption text-neutral-400">
              +{formatNumber(unattributedUserRequests)} request(s) not attributable to a known
              user (usage predating per-user attribution).
            </p>
          )}
        </TabsContent>
        <TabsContent value="key">
          <DataTable
            columns={keyColumns}
            rows={keyRows}
            getRowId={(k) => k.id}
            loading={isLoading}
            error={error}
            onRetry={onRetry}
          />
          {!isLoading && !error && unattributedKeyRequests > 0 && (
            <p className="mt-2 text-caption text-neutral-400">
              +{formatNumber(unattributedKeyRequests)} request(s) not attributable to a known API
              key (usage predating per-key attribution).
            </p>
          )}
        </TabsContent>
      </Tabs>
    </section>
  );
}

/** Actually calls useApiKeys() — only ever mounted under real auth (see UsageBreakdownSection
 * below), so the hook never fires the real /api/api-keys request under the mocked dev harness. */
function RealUsageBreakdown({
  visibleOwnerId,
}: {
  visibleOwnerId: string | null;
}) {
  const {
    data: keys,
    isLoading: keysLoading,
    error: keysError,
    refetch: refetchKeys,
  } = useApiKeys();
  const {
    data: users,
    isLoading: usersLoading,
    error: usersError,
  } = useUsers();
  const {
    data: byUser,
    isLoading: byUserLoading,
    error: byUserError,
  } = useUsageByUser();
  const {
    data: byKey,
    isLoading: byKeyLoading,
    error: byKeyError,
  } = useUsageByKey();

  const allKeys = keys ?? [];
  const scopedKeys = visibleOwnerId
    ? allKeys.filter((k) => k.owner_id === visibleOwnerId)
    : allKeys;

  const usersByUser = new Map(
    (byUser?.users ?? [])
      .filter((row): row is UsageByUserRow & { owner_id: string } => row.owner_id !== null)
      .map((row) => [row.owner_id, row]),
  );
  const usageByKeyMap = new Map(
    (byKey?.api_keys ?? [])
      .filter((row): row is UsageByKeyRow & { api_key_id: string } => row.api_key_id !== null)
      .map((row) => [row.api_key_id, row]),
  );
  const unattributedUserRequests =
    byUser?.users.find((row) => row.owner_id === null)?.requests ?? 0;
  const unattributedKeyRequests =
    byKey?.api_keys.find((row) => row.api_key_id === null)?.requests ?? 0;

  return (
    <UsageBreakdown
      keys={scopedKeys}
      users={users ?? []}
      usersByUser={usersByUser}
      usageByKey={usageByKeyMap}
      unattributedUserRequests={visibleOwnerId ? 0 : unattributedUserRequests}
      unattributedKeyRequests={visibleOwnerId ? 0 : unattributedKeyRequests}
      isLoading={keysLoading || usersLoading || byUserLoading || byKeyLoading}
      error={
        keysError?.message ??
        usersError?.message ??
        byUserError?.message ??
        byKeyError?.message ??
        null
      }
      onRetry={() => refetchKeys()}
    />
  );
}

/** Gated like ApiKeysPage: /api/api-keys is an end-user JWT surface with nothing to call under
 * the mocked dev harness. Branches to a DIFFERENT component rather than an early return after
 * calling the hook, so useApiKeys() is never invoked (and never fires its real request) when
 * real auth isn't active — an early return only skips the render, not the request. */
function UsageBreakdownSection({
  visibleOwnerId,
}: {
  visibleOwnerId: string | null;
}) {
  if (!shouldUseRealAuth()) {
    return (
      <section className="flex flex-col gap-3">
        <h2 className="text-sectionTitle font-semibold text-neutral-900">
          Usage breakdown
        </h2>
        <EmptyState
          title="Requires a real signed-in session"
          description="API keys are tied to your own identity, not the shared development key this environment is currently using. Sign in with VITE_REAL_AUTH enabled to see this breakdown."
        />
      </section>
    );
  }

  return <RealUsageBreakdown visibleOwnerId={visibleOwnerId} />;
}

/**
 * Billing & usage for the organization. Wired to every real read endpoint that exists: plan
 * limits and org status (org summary), org-wide usage totals + daily trend (usage summary /
 * timeseries), real per-user and per-API-key request/token counts (usage by-user / by-key,
 * add-per-user-key-usage-attribution), the real API-key list, and the real user directory. No
 * static/mock data remains — cost, invoices, payment method, and subscription status have no
 * read API anywhere in the backend, so those are left out of the page entirely rather than shown
 * as placeholders.
 */
export function BillingPage() {
  const { user } = useAuth();
  const [viewAs, setViewAs] = React.useState<ViewAs>("admin");
  const isAdmin = viewAs === "admin";

  const { data: summary } = useOrgSummary();

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Billing & Usage"
        description={
          isAdmin
            ? "Organization-wide usage and billing."
            : `Your usage as ${user.displayName}.`
        }
        actions={
          <div className="flex items-center gap-2">
            <span className="text-secondary text-neutral-500">View as</span>
            <Select
              aria-label="View as role"
              value={viewAs}
              onValueChange={(v) => setViewAs(v as ViewAs)}
              options={[
                { value: "admin", label: "Org admin" },
                { value: "member", label: "Member" },
              ]}
              className="w-36"
            />
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-3">
        {summary && <StatusBadge status={summary.organization.status} />}
        <Badge variant="info">{summary?.plan.name ?? "Plan —"}</Badge>
      </div>

      <PlanLimitsRow />
      <UsageKpiRow />
      <UsageOverTimeChart />
      <UsageBreakdownSection visibleOwnerId={isAdmin ? null : user.id} />
    </div>
  );
}
