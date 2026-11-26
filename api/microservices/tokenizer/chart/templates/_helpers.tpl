{{- define "tokenizer.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tokenizer.fullname" -}}
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

{{- define "tokenizer.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tokenizer.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tokenizer.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "tokenizer.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: platformforge-api
{{- end -}}

{{/*
The ServiceAccount this release's pods run as. A dedicated one rather than
"default": the default SA is shared by everything in the namespace, so any
RoleBinding granted to it silently applies here too. This one is bound to
nothing, which is the point -- none of these services call the Kubernetes
API.
*/}}
{{- define "tokenizer.serviceAccountName" -}}
{{- $sa := .Values.serviceAccount | default dict -}}
{{- if or (not (hasKey $sa "create")) $sa.create -}}
{{- default (include "tokenizer.fullname" .) ($sa.name | default "") -}}
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
{{- define "tokenizer.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}
