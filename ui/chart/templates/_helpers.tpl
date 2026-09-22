{{- define "portal.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "portal.fullname" -}}
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

{{- define "portal.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "portal.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "portal.selectorLabels" -}}
app.kubernetes.io/name: {{ include "portal.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The API the portal talks to. Neither set is refused rather than
defaulted: the bundle calls /v1 on its own origin, so with no proxy the
portal loads, every request 404s, and the login it opens on cannot
complete -- which looks exactly like a working deployment until someone
tries to sign in.
*/}}
{{- define "portal.apiUpstream" -}}
{{- if .Values.config.apiUpstream -}}
{{- .Values.config.apiUpstream -}}
{{- else if not .Values.config.allowNoApi -}}
{{- fail "\n\nportal: config.apiUpstream is not set.\n\nThe bundle calls its API on the same origin under /v1\n(src/api/client.ts), so without this proxy the portal serves, every\nrequest 404s, and the login page it opens on cannot complete.\n\nSet it:\n  config.apiUpstream=http://organization-control-plane.<ns>.svc.cluster.local:8000\nor say the API is deliberately absent:\n  config.allowNoApi=true\n" -}}
{{- end -}}
{{- end -}}
