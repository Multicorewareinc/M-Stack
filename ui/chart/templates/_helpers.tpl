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

{{/*
The ServiceAccount this release's pods run as. A dedicated one rather than
"default": the default SA is shared by everything in the namespace, so any
RoleBinding granted to it silently applies here too. This one is bound to
nothing, which is the point -- none of these services call the Kubernetes
API.
*/}}
{{- define "portal.serviceAccountName" -}}
{{- $sa := .Values.serviceAccount | default dict -}}
{{- if or (not (hasKey $sa "create")) $sa.create -}}
{{- default (include "portal.fullname" .) ($sa.name | default "") -}}
{{- else -}}
{{- default "default" ($sa.name | default "") -}}
{{- end -}}
{{- end -}}

{{/*
The image reference. A digest wins over a tag when both are set: a tag is
mutable, so "0.1.0" on one node can be a different build from "0.1.0" on
another after a re-push, and with imagePullPolicy IfNotPresent a node that
already cached it never finds out. A digest is the only form that names
one specific build.
*/}}
{{- define "portal.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}
