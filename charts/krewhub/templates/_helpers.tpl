{{/*
Chart name/version -- só usado pro label helm.sh/chart. Nomes de
RECURSO (Deployment/Service/PVC/etc.) NÃO usam um "fullname" genérico
tipo <release>-<chart> -- são fixos (ou vêm de .Values.*.name),
casando de propósito com os nomes já em produção
(krewhub-central, configurable-http-proxy, krewhub-central-data, ...)
pra um `helm template`/futura adoção não forçar replace de recursos por
mudança de nome. Ver AGENTS.md, seção "Project conventions".
*/}}
{{- define "krewhub.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Labels comuns a TODOS os recursos deste chart.
*/}}
{{- define "krewhub.labels" -}}
helm.sh/chart: {{ include "krewhub.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: krewhub
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end }}

{{/*
Labels/selector do krewhub-central -- iguais ao "app: krewhub-central"
já em produção (não usa app.kubernetes.io/name pra não mudar o selector
de um Deployment já rodando, o que forçaria recriação).
*/}}
{{- define "krewhubCentral.selectorLabels" -}}
app: krewhub-central
{{- end }}

{{/*
Labels/selector do CHP -- iguais ao "app: configurable-http-proxy" já em
produção, mesmo motivo acima.
*/}}
{{- define "chp.selectorLabels" -}}
app: configurable-http-proxy
{{- end }}

{{/*
Nome do ServiceAccount do krewhub-central.
*/}}
{{- define "krewhubCentral.serviceAccountName" -}}
{{- if .Values.krewhubCentral.serviceAccount.create }}
{{- default "krewhub-central" .Values.krewhubCentral.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.krewhubCentral.serviceAccount.name }}
{{- end }}
{{- end }}
