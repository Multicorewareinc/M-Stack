{{/* Chart name, overridable. */}}
{{- define "model-gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified name. Release-scoped, so two releases in one namespace do
not collide -- except when the release is already named after the chart,
where "gateway-model-gateway" would just be noise.
*/}}
{{- define "model-gateway.fullname" -}}
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

{{- define "model-gateway.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "model-gateway.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: platformforge-api
{{- end -}}

{{- define "model-gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "model-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The Secret holding API_KEY, UPSTREAM_API_KEY and MODEL_ROUTES.

Credentials never travel in a values file in normal use, so
existingSecret is the intended path and secret.create exists for local
work. Neither set is an error rather than a default, because the
alternative is a gateway that silently authenticates on the image's
built-in development key.
*/}}
{{- define "model-gateway.secretName" -}}
{{- if .Values.secret.existingSecret -}}
{{- .Values.secret.existingSecret -}}
{{- else if .Values.secret.create -}}
{{- include "model-gateway.fullname" . -}}
{{- else -}}
{{- fail "\n\nmodel-gateway: no API key source configured.\n\nSet one of:\n  secret.existingSecret=<name>   a Secret you created, holding API_KEY\n                                 (and optionally UPSTREAM_API_KEY, MODEL_ROUTES)\n  secret.create=true             have this chart create it from values\n                                 -- for local work only: the key is then in\n                                 your values file and in `helm get values`.\n\nThe gateway image defaults to the key 'pf-local-dev-key', so deploying\nwithout a Secret would put a published default credential in front of\nyour inference server.\n" -}}
{{- end -}}
{{- end -}}
