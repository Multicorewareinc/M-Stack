{{/* Chart name, overridable. */}}
{{- define "billing.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified name. Release-scoped, so two releases in one namespace do
not collide -- except when the release is already named after the chart,
where "platform-billing" would just be noise.
*/}}
{{- define "billing.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "billing.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "billing.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: platformforge-api
{{- end -}}

{{- define "billing.selectorLabels" -}}
app.kubernetes.io/name: {{ include "billing.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The Secret holding DATABASE_URL, SERVICE_API_KEY, and optionally
STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET. All are credentials:
DATABASE_URL embeds the DB user/password, SERVICE_API_KEY is a bearer
token shared with admin-control-plane, and the Stripe values are live
API credentials when set.
*/}}
{{- define "billing.secretName" -}}
{{- if .Values.secret.existingSecret -}}
{{- .Values.secret.existingSecret -}}
{{- else -}}
{{- include "billing.fullname" . -}}
{{- end -}}
{{- end -}}
