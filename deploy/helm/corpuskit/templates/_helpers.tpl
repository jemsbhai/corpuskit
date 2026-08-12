{{- define "corpuskit.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "corpuskit.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "corpuskit.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "corpuskit.labels" -}}
app.kubernetes.io/name: {{ include "corpuskit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "corpuskit.selectorLabels" -}}
app.kubernetes.io/name: {{ include "corpuskit.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "corpuskit.image" -}}
{{- printf "%s@%s" .image.repository .image.digest -}}
{{- end -}}

{{- define "corpuskit.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "corpuskit.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
readOnlyRootFilesystem: true
runAsNonRoot: true
{{- end -}}

{{- define "corpuskit.dnsEgress" -}}
- to:
    - namespaceSelector:
        matchLabels: {{- toYaml .Values.networkPolicy.dns.namespaceSelector | nindent 10 }}
      podSelector:
        matchLabels: {{- toYaml .Values.networkPolicy.dns.podSelector | nindent 10 }}
  ports:
    - {protocol: UDP, port: 53}
    - {protocol: TCP, port: 53}
{{- end -}}

{{- define "corpuskit.cidrEgress" -}}
{{- $port := .port -}}
{{- range .cidrs }}
- to:
    - ipBlock:
        cidr: {{ . | quote }}
  ports:
    - protocol: TCP
      port: {{ $port }}
{{- end }}
{{- end -}}

{{- define "corpuskit.validate" -}}
{{- $zero := "sha256:0000000000000000000000000000000000000000000000000000000000000000" -}}
{{- range $name, $image := omit .Values.images "pullPolicy" -}}
  {{- $repository := required (printf "images.%s.repository is required" $name) $image.repository -}}
  {{- $digest := required (printf "images.%s.digest is required" $name) $image.digest -}}
  {{- if eq $image.digest $zero }}{{ fail (printf "images.%s.digest must be a real release digest, not the all-zero placeholder" $name) }}{{ end -}}
{{- end -}}
{{- if not .Values.networkPolicy.enabled }}{{ fail "networkPolicy.enabled must be true" }}{{ end -}}
{{- if not .Values.ingress.enabled }}{{ fail "ingress.enabled must be true for the production chart" }}{{ end -}}
{{- if ne (index .Values.global.nodeSelector "kubernetes.io/os") "linux" }}{{ fail "global.nodeSelector must enforce kubernetes.io/os=linux" }}{{ end -}}
{{- if ne (index .Values.global.nodeSelector "kubernetes.io/arch") "amd64" }}{{ fail "global.nodeSelector must enforce kubernetes.io/arch=amd64" }}{{ end -}}
{{- $ingressHost := required "ingress.host is required" .Values.ingress.host -}}
{{- $ingressTls := required "ingress.tlsSecretName is required" .Values.ingress.tlsSecretName -}}
{{- if not .Values.temporal.tls }}{{ fail "temporal.tls must be true" }}{{ end -}}
{{- if not (hasSuffix (printf ":%v" .Values.temporal.port) .Values.temporal.address) }}{{ fail "temporal.address port must exactly match temporal.port" }}{{ end -}}
{{- $https443Url := "^https://[A-Za-z0-9.-]+(:443)?(/[^?#]*)?$" -}}
{{- $https443Origin := "^https://[A-Za-z0-9.-]+(:443)?/?$" -}}
{{- if not (regexMatch $https443Url .Values.oidc.issuer) }}{{ fail "oidc.issuer must use HTTPS on port 443" }}{{ end -}}
{{- if not (regexMatch $https443Url .Values.oidc.redirectUri) }}{{ fail "oidc.redirectUri must use HTTPS on port 443" }}{{ end -}}
{{- if not (regexMatch $https443Origin .Values.artifactStorage.endpoint) }}{{ fail "artifactStorage.endpoint must use HTTPS on port 443" }}{{ end -}}
{{- if not (hasPrefix "https://" .Values.monitoring.runbookBaseUrl) }}{{ fail "monitoring.runbookBaseUrl must be an HTTPS URL" }}{{ end -}}
{{- if not .Values.monitoring.enabled }}{{ fail "monitoring.enabled must be true" }}{{ end -}}
{{- if not .Values.monitoring.contractAlertsEnabled }}{{ fail "monitoring.contractAlertsEnabled must be true" }}{{ end -}}
{{- range $kind := list "database" "temporal" "artifactStorage" "redis" "oidc" -}}
  {{- if eq (len (index $.Values.networkPolicy.cidrs $kind)) 0 }}{{ fail (printf "networkPolicy.cidrs.%s requires at least one explicit CIDR" $kind) }}{{ end -}}
{{- end -}}
{{- if and .Values.workers.externalProvider.enabled (eq (len .Values.networkPolicy.cidrs.providers) 0) }}{{ fail "networkPolicy.cidrs.providers is required when the external-provider worker is enabled" }}{{ end -}}
{{- $dbSeen := dict -}}
{{- range $name, $ref := omit .Values.database "port" -}}
  {{- $databaseSecret := required (printf "database.%s.secretName is required" $name) $ref.secretName -}}
  {{- if hasKey $dbSeen $ref.secretName }}{{ fail (printf "database credentials must be distinct; Secret %s is reused" $ref.secretName) }}{{ end -}}
  {{- $_ := set $dbSeen $ref.secretName true -}}
{{- end -}}
{{- if and (eq .Values.artifactStorage.encryption "aws:kms") (not .Values.artifactStorage.kmsKeyId.secretName) }}{{ fail "artifactStorage.kmsKeyId is required for aws:kms" }}{{ end -}}
{{- if and .Values.monitoring.otelCollector.enabled (not (regexMatch $https443Url .Values.monitoring.otelCollector.exporterEndpoint)) }}{{ fail "monitoring.otelCollector.exporterEndpoint must use HTTPS on port 443" }}{{ end -}}
{{- if and .Values.monitoring.otelCollector.enabled (eq .Values.monitoring.otelCollector.image.digest "") }}{{ fail "monitoring.otelCollector.image.digest is required" }}{{ end -}}
{{- if and .Values.monitoring.otelCollector.enabled (eq .Values.monitoring.otelCollector.image.digest $zero) }}{{ fail "monitoring.otelCollector.image.digest must not be the all-zero placeholder" }}{{ end -}}
{{- if and .Values.monitoring.otelCollector.enabled (eq (len .Values.networkPolicy.cidrs.telemetryExporter) 0) }}{{ fail "networkPolicy.cidrs.telemetryExporter is required when the OTLP exporter is enabled" }}{{ end -}}
{{- if and (not .Values.phoible.sourceClaim) (eq (len .Values.networkPolicy.cidrs.phoibleSource) 0) }}{{ fail "networkPolicy.cidrs.phoibleSource is required when no offline PHOIBLE source claim is configured" }}{{ end -}}
{{- range $key := list "kubernetes.io/ingress.class" "nginx.ingress.kubernetes.io/ssl-redirect" "nginx.ingress.kubernetes.io/force-ssl-redirect" "nginx.ingress.kubernetes.io/proxy-body-size" "nginx.ingress.kubernetes.io/proxy-read-timeout" -}}
  {{- if hasKey $.Values.ingress.annotations $key }}{{ fail (printf "ingress.annotations must not override chart-owned key %s" $key) }}{{ end -}}
{{- end -}}
{{- range $key := list "app.kubernetes.io/name" "app.kubernetes.io/instance" "app.kubernetes.io/component" "app.kubernetes.io/managed-by" "app.kubernetes.io/version" "helm.sh/chart" "corpuskit.io/worker-profile" -}}
  {{- if hasKey $.Values.global.podLabels $key }}{{ fail (printf "global.podLabels must not override chart-owned key %s" $key) }}{{ end -}}
{{- end -}}
{{- range $key := list "checksum/config" "checksum/runtime-policy" "helm.sh/hook" "helm.sh/hook-weight" "helm.sh/hook-delete-policy" -}}
  {{- if hasKey $.Values.global.podAnnotations $key }}{{ fail (printf "global.podAnnotations must not override chart-owned key %s" $key) }}{{ end -}}
{{- end -}}
{{- $phoibleClaim := required "phoible.cacheClaim is required" .Values.phoible.cacheClaim -}}
{{- $modelClaim := required "workers.common.modelCacheClaim is required" .Values.workers.common.modelCacheClaim -}}
{{- $datgClaim := required "workers.common.datgIndexCacheClaim is required" .Values.workers.common.datgIndexCacheClaim -}}
{{- $saSeen := dict -}}
{{- range $component, $name := .Values.serviceAccounts -}}
  {{- if hasKey $saSeen $name }}{{ fail (printf "service account names must be distinct; %s is reused" $name) }}{{ end -}}
  {{- $_ := set $saSeen $name true -}}
{{- end -}}
{{- $s3Seen := dict -}}
{{- range $component, $ref := .Values.artifactStorage.credentials -}}
  {{- if hasKey $s3Seen $ref.secretName }}{{ fail (printf "artifact credentials must be distinct; Secret %s is reused" $ref.secretName) }}{{ end -}}
  {{- $_ := set $s3Seen $ref.secretName true -}}
{{- end -}}
{{- $temporalSeen := dict -}}
{{- range $component, $ref := .Values.temporal.credentials -}}
  {{- if hasKey $temporalSeen $ref.secretName }}{{ fail (printf "Temporal credentials must be distinct; Secret %s is reused" $ref.secretName) }}{{ end -}}
  {{- $_ := set $temporalSeen $ref.secretName true -}}
{{- end -}}
{{- $canonicalReturns := list "/" "/projects" "/evaluate" "/analysis" "/capabilities" "/g2p" "/inventory" "/coverage" "/selection" "/generation" "/advanced" "/jobs" "/artifacts" -}}
{{- if not (deepEqual .Values.web.allowedReturnPaths $canonicalReturns) }}{{ fail "web.allowedReturnPaths must exactly match the mounted production route allowlist" }}{{ end -}}
{{- range $name, $profile := dict "gpuInference" .Values.workers.gpuInference "gpuTraining" .Values.workers.gpuTraining -}}
  {{- range $key, $_ := $profile.nodeSelector -}}
    {{- if hasKey $.Values.global.nodeSelector $key }}{{ fail (printf "workers.%s.nodeSelector must not override global node selector %s" $name $key) }}{{ end -}}
  {{- end -}}
  {{- $requestGpu := required (printf "workers.%s.resources.requests[nvidia.com/gpu] is required" $name) (index $profile.resources.requests "nvidia.com/gpu") | toString -}}
  {{- $limitGpu := required (printf "workers.%s.resources.limits[nvidia.com/gpu] is required" $name) (index $profile.resources.limits "nvidia.com/gpu") | toString -}}
  {{- if or (eq $requestGpu "0") (ne $requestGpu $limitGpu) }}{{ fail (printf "workers.%s GPU request and limit must be equal and nonzero" $name) }}{{ end -}}
{{- end -}}
{{- range $name, $profile := dict "batch" .Values.workers.batch "externalProvider" .Values.workers.externalProvider -}}
  {{- if or (hasKey $profile.resources.requests "nvidia.com/gpu") (hasKey $profile.resources.limits "nvidia.com/gpu") }}{{ fail (printf "workers.%s must not request GPU resources" $name) }}{{ end -}}
{{- end -}}
{{- $providerNames := dict -}}
{{- range $secret := .Values.workers.externalProvider.providerSecrets -}}
  {{- if hasKey $providerNames $secret.name }}{{ fail (printf "provider secret environment name %s is duplicated" $secret.name) }}{{ end -}}
  {{- $_ := set $providerNames $secret.name false -}}
{{- end -}}
{{- range $policy := .Values.workers.externalProvider.hostedModelPolicies -}}
  {{- if ne (first (splitList "/" $policy.model)) $policy.provider }}{{ fail (printf "hosted model namespace %s must exactly match provider %s" $policy.model $policy.provider) }}{{ end -}}
  {{- $envName := trimPrefix "secret://env/" $policy.credential_ref.reference -}}
  {{- if not (hasKey $providerNames $envName) }}{{ fail (printf "hosted policy credential reference %s has no exact providerSecrets entry" $policy.credential_ref.reference) }}{{ end -}}
  {{- $_ := set $providerNames $envName true -}}
  {{- $promptIds := dict -}}
  {{- $promptRefs := dict -}}
  {{- range $prompt := (default (list) $policy.prompt_templates) -}}
    {{- $promptName := trimPrefix "secret://env/" $prompt.template_ref.reference -}}
    {{- if eq $promptName $envName }}{{ fail "hosted credentials and prompt templates require distinct Secrets" }}{{ end -}}
    {{- if hasKey $promptIds $prompt.template_id }}{{ fail (printf "hosted prompt template ID %s is duplicated" $prompt.template_id) }}{{ end -}}
    {{- if hasKey $promptRefs $promptName }}{{ fail (printf "hosted prompt template Secret %s is duplicated" $promptName) }}{{ end -}}
    {{- if not (hasKey $providerNames $promptName) }}{{ fail (printf "hosted prompt template reference %s has no exact providerSecrets entry" $prompt.template_ref.reference) }}{{ end -}}
    {{- if lt (int $prompt.max_rendered_bytes) (int $prompt.size_bytes) }}{{ fail (printf "hosted prompt template %s rendered-byte ceiling is too small" $prompt.template_id) }}{{ end -}}
    {{- $_ := set $promptIds $prompt.template_id true -}}
    {{- $_ := set $promptRefs $promptName true -}}
    {{- $_ := set $providerNames $promptName true -}}
  {{- end -}}
{{- end -}}
{{- range $name, $used := $providerNames -}}
  {{- if not $used }}{{ fail (printf "providerSecrets entry %s is not referenced by a hosted model policy" $name) }}{{ end -}}
{{- end -}}
{{- $huggingFaceSelectors := dict -}}
{{- range $policy := .Values.workers.externalProvider.huggingFaceRepositoryPolicies -}}
  {{- $selector := toJson (list $policy.dataset $policy.config $policy.split $policy.text_column $policy.revision $policy.language) -}}
  {{- if hasKey $huggingFaceSelectors $selector }}{{ fail (printf "Hugging Face repository selector %s is duplicated" $selector) }}{{ end -}}
  {{- $_ := set $huggingFaceSelectors $selector true -}}
{{- end -}}
{{- range $policy := .Values.workers.gpuInference.localModelPolicies -}}
  {{- if not (has "cuda" $policy.allowed_devices) }}{{ fail "gpuInference local model policies must allow the cuda device" }}{{ end -}}
{{- end -}}
{{- if or (eq (len .Values.workers.batch.datgRuntimePolicies) 0) (eq (len .Values.workers.gpuInference.datgRuntimePolicies) 0) }}{{ fail "matching batch and gpuInference DATG runtime policies are required" }}{{ end -}}
{{- if not (deepEqual .Values.workers.batch.datgRuntimePolicies .Values.workers.gpuInference.datgRuntimePolicies) }}{{ fail "batch and gpuInference DATG runtime policies must be identical" }}{{ end -}}
{{- $datgRuntimeIds := dict -}}
{{- range $policy := .Values.workers.gpuInference.datgRuntimePolicies -}}
  {{- if hasKey $datgRuntimeIds $policy.runtime_id }}{{ fail (printf "DATG runtime policy ID %s is duplicated" $policy.runtime_id) }}{{ end -}}
  {{- $_ := set $datgRuntimeIds $policy.runtime_id true -}}
  {{- if not (deepEqual $policy.model $policy.tokenizer) }}{{ fail (printf "DATG policy %s must pin identical model and tokenizer snapshots" $policy.runtime_id) }}{{ end -}}
{{- end -}}
{{- range $policy := .Values.workers.gpuTraining.phonRlRuntimePolicies -}}
  {{- if not (deepEqual $policy.model $policy.tokenizer) }}{{ fail (printf "Phon-RL policy %s must pin identical model and tokenizer snapshots" $policy.runtime_id) }}{{ end -}}
  {{- if not (hasKey $.Values.workers.gpuTraining.phonRlCacheRoots $policy.cache_root_id) }}{{ fail (printf "Phon-RL policy %s references an unmapped cache root" $policy.runtime_id) }}{{ end -}}
{{- end -}}
{{- end -}}
