{{/* Chart name, overridable. */}}
{{- define "admin-control-plane.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified name. Release-scoped, so two releases in one namespace do
not collide -- except when the release is already named after the chart,
where "platform-admin-control-plane" would just be noise.
*/}}
{{- define "admin-control-plane.fullname" -}}
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

{{- define "admin-control-plane.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "admin-control-plane.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: platformforge-api
{{- end -}}

{{- define "admin-control-plane.selectorLabels" -}}
app.kubernetes.io/name: {{ include "admin-control-plane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The Secret holding DATABASE_URL_ADMIN, ADMIN_API_KEY and SERVICE_API_KEY.
All three are credentials: DATABASE_URL_ADMIN embeds the DB user/password,
the two keys are bearer tokens.
*/}}
{{/*
Credentials never travel in a values file in normal use, so
existingSecret is the intended path and secret.create exists for local
work. Neither set is refused here rather than defaulted, because the
name is mounted by both the Deployment and the migration Job.
*/}}
{{- define "admin-control-plane.secretName" -}}
{{- if .Values.secret.existingSecret -}}
{{- .Values.secret.existingSecret -}}
{{- else if .Values.secret.create -}}
{{- include "admin-control-plane.fullname" . -}}
{{- else -}}
{{- fail "\n\nadmin-control-plane: no Secret source configured.\n\nSet one of:\n  secret.existingSecret=<name>   a Secret you created, holding\n                                 DATABASE_URL_ADMIN, ADMIN_API_KEY and\n                                 SERVICE_API_KEY\n  secret.create=true             have this chart create it from values\n                                 -- for local work only: they then sit\n                                 in your values file and in\n                                 `helm get values`.\n\nNeither set is an error rather than a default. The Deployment and the\npre-install migration Job both mount this Secret, so without it the\ninstall renders cleanly and then dies on the hook with a\nCreateContainerConfigError, which says nothing about what is missing.\n" -}}
{{- end -}}
{{- end -}}

{{/*
The ServiceAccount this release's pods run as. A dedicated one rather than
"default": the default SA is shared by everything in the namespace, so any
RoleBinding granted to it silently applies here too. This one is bound to
nothing, which is the point -- none of these services call the Kubernetes
API.
*/}}
{{- define "admin-control-plane.serviceAccountName" -}}
{{- $sa := .Values.serviceAccount | default dict -}}
{{- if or (not (hasKey $sa "create")) $sa.create -}}
{{- default (include "admin-control-plane.fullname" .) ($sa.name | default "") -}}
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
{{- define "admin-control-plane.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}
