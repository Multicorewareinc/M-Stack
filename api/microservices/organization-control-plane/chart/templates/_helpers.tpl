{{/* Chart name, overridable. */}}
{{- define "organization-control-plane.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified name. Release-scoped, so two releases in one namespace do
not collide -- except when the release is already named after the chart,
where "platform-organization-control-plane" would just be noise.
*/}}
{{- define "organization-control-plane.fullname" -}}
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

{{- define "organization-control-plane.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "organization-control-plane.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: platformforge-api
{{- end -}}

{{- define "organization-control-plane.selectorLabels" -}}
app.kubernetes.io/name: {{ include "organization-control-plane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The Secret holding DATABASE_URL and SERVICE_API_KEY. Both are
credentials: DATABASE_URL embeds the DB user/password, SERVICE_API_KEY
is a bearer token shared with admin-control-plane.
*/}}
{{/*
Credentials never travel in a values file in normal use, so
existingSecret is the intended path and secret.create exists for local
work. Neither set is refused here rather than defaulted, because the
name is mounted by both the Deployment and the migration Job.
*/}}
{{- define "organization-control-plane.secretName" -}}
{{- if .Values.secret.existingSecret -}}
{{- .Values.secret.existingSecret -}}
{{- else if .Values.secret.create -}}
{{- include "organization-control-plane.fullname" . -}}
{{- else -}}
{{- fail "\n\norganization-control-plane: no Secret source configured.\n\nSet one of:\n  secret.existingSecret=<name>   a Secret you created, holding\n                                 DATABASE_URL and SERVICE_API_KEY\n  secret.create=true             have this chart create it from values\n                                 -- for local work only: they then sit\n                                 in your values file and in\n                                 `helm get values`.\n\nNeither set is an error rather than a default. The Deployment and the\npre-install migration Job both mount this Secret, so without it the\ninstall renders cleanly and then dies on the hook with a\nCreateContainerConfigError, which says nothing about what is missing.\n" -}}
{{- end -}}
{{- end -}}
