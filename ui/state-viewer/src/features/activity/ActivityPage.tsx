import {
  Badge,
  DataTable,
  PageHeader,
  Select,
  type Column,
} from "@multistack/ui";
import * as React from "react";
import type { FeedEntry } from "../../api/client";
import { formatDateTime, HEALTH_STATUSES, healthMeta } from "../../lib/format";
import { useSelectedDeployment } from "../deployments/DeploymentDetail";
import { StateGate } from "../state/StateGate";
import { useHealthChecks } from "../state/hooks";

const ALL = "all";
const LIMIT = 200;

function ActivityView() {
  const [status, setStatus] = React.useState(ALL);
  const { select } = useSelectedDeployment();
  const { data, isLoading, error, refetch } = useHealthChecks({
    status: status === ALL ? undefined : status,
    limit: LIMIT,
  });

  const columns: Column<FeedEntry>[] = [
    {
      key: "time",
      header: "Time",
      accessor: (e) => formatDateTime(e.checked_at),
    },
    {
      key: "name",
      header: "Deployment",
      render: (e) => <span className="font-medium">{e.deployment_name}</span>,
    },
    {
      key: "layer",
      header: "Layer",
      accessor: (e) => e.component_type ?? "deleted",
    },
    {
      key: "status",
      header: "Result",
      render: (e) => (
        <Badge variant={healthMeta(e.status).variant}>
          {healthMeta(e.status).label}
        </Badge>
      ),
    },
    {
      key: "detail",
      header: "Detail",
      render: (e) => <span className="line-clamp-2 max-w-xl">{e.detail}</span>,
    },
  ];

  return (
    <div>
      <PageHeader
        title="Activity"
        description="The most recent outcomes recorded across every deployment, newest first."
      />
      <div className="mb-4 flex items-center gap-3">
        <Select
          aria-label="Filter by result"
          value={status}
          onValueChange={setStatus}
          options={[
            { value: ALL, label: "All results" },
            ...HEALTH_STATUSES.map((s) => ({
              value: s,
              label: healthMeta(s).label,
            })),
          ]}
          className="w-52"
        />
        <span className="text-caption text-neutral-500">
          Showing up to {LIMIT}
        </span>
      </div>
      <DataTable
        columns={columns}
        rows={data ?? []}
        getRowId={(e) => String(e.id)}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        onRowClick={(e) => select(e.deployment_name)}
        empty={{
          title: "No activity yet",
          description: "Results appear here as deployments finish or fail.",
        }}
      />
    </div>
  );
}

export function ActivityPage() {
  return (
    <StateGate>
      <ActivityView />
    </StateGate>
  );
}
