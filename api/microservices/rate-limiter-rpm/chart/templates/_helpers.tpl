{{/* Chart name, overridable. */}}
{{- define "rate-limiter-rpm.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified name. Release-scoped, so two releases in one namespace do
not collide -- except when the release is already named after the chart,
where "gateway-model-gateway" would just be noise.
*/}}
{{- define "rate-limiter-rpm.fullname" -}}
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

{{- define "rate-limiter-rpm.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "rate-limiter-rpm.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: platformforge-api
{{- end -}}

{{- define "rate-limiter-rpm.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rate-limiter-rpm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The Secret holding RATE_LIMITS.

RATE_LIMITS is not merely configuration. Its user_overrides and
user_model_overrides maps are keyed by *principal*, and the gateway sets
the principal to the caller's bearer token
(model-gateway/src/dependencies.py). So a limits document that gives one
customer a higher ceiling contains that customer's API key as a map key.

A limits document with only model-scoped entries carries no credential,
but the shape does not distinguish them at deploy time, so both go in
the Secret.
*/}}
{{- define "rate-limiter-rpm.secretName" -}}
{{- if .Values.secret.existingSecret -}}
{{- .Values.secret.existingSecret -}}
{{- else -}}
{{- include "rate-limiter-rpm.fullname" . -}}
{{- end -}}
{{- end -}}
