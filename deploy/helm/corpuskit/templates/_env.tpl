{{- define "corpuskit.baseEnv" -}}
- name: CORPUSKIT_ENVIRONMENT
  value: {{ .root.Values.global.environment | quote }}
- name: CORPUSKIT_RUNTIME_ROLE
  value: {{ .role | quote }}
- name: CORPUSKIT_LOG_LEVEL
  value: {{ .root.Values.global.logLevel | quote }}
{{- end -}}

{{- define "corpuskit.databaseEnv" -}}
- name: CORPUSKIT_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .secretName }}
      key: {{ .key }}
{{- end -}}

{{- define "corpuskit.temporalEnv" -}}
{{- $root := .root -}}
- name: CORPUSKIT_JOB_BACKEND
  value: temporal
- name: CORPUSKIT_TEMPORAL_ADDRESS
  value: {{ $root.Values.temporal.address | quote }}
- name: CORPUSKIT_TEMPORAL_NAMESPACE
  value: {{ $root.Values.temporal.namespace | quote }}
- name: CORPUSKIT_TEMPORAL_TLS
  value: "true"
- name: CORPUSKIT_TEMPORAL_TASK_QUEUE
  value: {{ .profile | quote }}
- name: CORPUSKIT_WORKER_PROFILE
  value: {{ .profile | quote }}
- name: CORPUSKIT_TEMPORAL_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .credentials.secretName }}
      key: {{ .credentials.key }}
{{- end -}}

{{- define "corpuskit.artifactEnv" -}}
{{- $root := .root -}}
- name: CORPUSKIT_ARTIFACT_BACKEND
  value: s3
- name: CORPUSKIT_ARTIFACT_S3_ENDPOINT
  value: {{ $root.Values.artifactStorage.endpoint | quote }}
- name: CORPUSKIT_ARTIFACT_S3_BUCKET
  value: {{ $root.Values.artifactStorage.bucket | quote }}
- name: CORPUSKIT_ARTIFACT_S3_REGION
  value: {{ $root.Values.artifactStorage.region | quote }}
- name: CORPUSKIT_ARTIFACT_S3_PATH_STYLE
  value: {{ $root.Values.artifactStorage.pathStyle | quote }}
- name: CORPUSKIT_ARTIFACT_S3_SSE
  value: {{ $root.Values.artifactStorage.encryption | quote }}
- name: CORPUSKIT_ARTIFACT_S3_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ .credentials.secretName }}
      key: {{ .credentials.accessKey }}
- name: CORPUSKIT_ARTIFACT_S3_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .credentials.secretName }}
      key: {{ .credentials.secretKey }}
{{- if eq $root.Values.artifactStorage.encryption "aws:kms" }}
- name: CORPUSKIT_ARTIFACT_S3_KMS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ $root.Values.artifactStorage.kmsKeyId.secretName }}
      key: {{ $root.Values.artifactStorage.kmsKeyId.key }}
{{- end }}
{{- end -}}

{{- define "corpuskit.apiEnv" -}}
{{- $root := .root -}}
{{- include "corpuskit.baseEnv" (dict "root" $root "role" "api") }}
{{ include "corpuskit.databaseEnv" .database }}
- name: CORPUSKIT_AUTH_MODE
  value: oidc
- name: CORPUSKIT_API_DOCS_ENABLED
  value: "false"
- name: CORPUSKIT_METRICS_ENABLED
  value: "true"
- name: CORPUSKIT_API_RATE_LIMIT_ENABLED
  value: {{ $root.Values.api.rateLimit.enabled | quote }}
- name: CORPUSKIT_API_RATE_LIMIT_WINDOW_SECONDS
  value: {{ $root.Values.api.rateLimit.windowSeconds | quote }}
- name: CORPUSKIT_API_RATE_LIMIT_READ_REQUESTS
  value: {{ $root.Values.api.rateLimit.readRequests | quote }}
- name: CORPUSKIT_API_RATE_LIMIT_WRITE_REQUESTS
  value: {{ $root.Values.api.rateLimit.writeRequests | quote }}
- name: CORPUSKIT_API_RATE_LIMIT_RETENTION_WINDOWS
  value: {{ $root.Values.api.rateLimit.retentionWindows | quote }}
- name: CORPUSKIT_METRICS_BEARER_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ $root.Values.metrics.bearerToken.secretName }}
      key: {{ $root.Values.metrics.bearerToken.key }}
- name: CORPUSKIT_OIDC_ISSUER
  value: {{ $root.Values.oidc.issuer | quote }}
- name: CORPUSKIT_OIDC_AUDIENCE
  value: {{ $root.Values.oidc.audience | quote }}
- name: CORPUSKIT_ALLOWED_ORIGINS
  value: {{ toJson $root.Values.api.allowedOrigins | quote }}
- name: CORPUSKIT_REQUIRED_CAPABILITIES
  value: {{ toJson $root.Values.api.requiredCapabilities | quote }}
- name: CORPUSKIT_WORKER_HOSTED_MODEL_POLICIES
  value: {{ toJson $root.Values.workers.externalProvider.hostedModelPolicies | quote }}
- name: CORPUSKIT_WORKER_HUGGINGFACE_REPOSITORY_POLICIES
  value: {{ toJson $root.Values.workers.externalProvider.huggingFaceRepositoryPolicies | quote }}
- name: CORPUSKIT_WORKER_LOCAL_MODEL_POLICIES
  value: {{ toJson $root.Values.workers.gpuInference.localModelPolicies | quote }}
- name: CORPUSKIT_WORKER_DATG_RUNTIME_POLICIES
  value: {{ toJson $root.Values.workers.gpuInference.datgRuntimePolicies | quote }}
- name: CORPUSKIT_WORKER_PHON_RL_RUNTIME_POLICIES
  value: {{ toJson $root.Values.workers.gpuTraining.phonRlRuntimePolicies | quote }}
{{ include "corpuskit.temporalEnv" (dict "root" $root "profile" "batch-cpu" "credentials" $root.Values.temporal.credentials.api) }}
{{ include "corpuskit.artifactEnv" (dict "root" $root "credentials" .artifactCredentials) }}
{{- end -}}

{{- define "corpuskit.dispatcherEnv" -}}
{{- include "corpuskit.baseEnv" (dict "root" .root "role" "dispatcher") }}
{{ include "corpuskit.databaseEnv" .database }}
{{ include "corpuskit.temporalEnv" (dict "root" .root "profile" "batch-cpu" "credentials" .root.Values.temporal.credentials.dispatcher) }}
{{- end -}}

{{- define "corpuskit.workerEnv" -}}
{{- include "corpuskit.baseEnv" (dict "root" .root "role" "worker") }}
{{ include "corpuskit.databaseEnv" .database }}
- name: CORPUSKIT_ADOPTION_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .adoptionDatabase.secretName }}
      key: {{ .adoptionDatabase.key }}
{{ include "corpuskit.temporalEnv" (dict "root" .root "profile" .profile "credentials" .temporalCredentials) }}
{{ include "corpuskit.artifactEnv" (dict "root" .root "credentials" .artifactCredentials) }}
- name: CORPUSKIT_ARTIFACT_MAX_BYTES
  value: {{ .root.Values.workers.common.artifactMaxBytes | quote }}
{{- end -}}

{{- define "corpuskit.maintenanceEnv" -}}
{{- include "corpuskit.baseEnv" (dict "root" .root "role" "maintenance") }}
{{ include "corpuskit.databaseEnv" .database }}
{{ include "corpuskit.artifactEnv" (dict "root" .root "credentials" .artifactCredentials) }}
- name: CORPUSKIT_API_RATE_LIMIT_WINDOW_SECONDS
  value: {{ .root.Values.api.rateLimit.windowSeconds | quote }}
- name: CORPUSKIT_API_RATE_LIMIT_READ_REQUESTS
  value: {{ .root.Values.api.rateLimit.readRequests | quote }}
- name: CORPUSKIT_API_RATE_LIMIT_WRITE_REQUESTS
  value: {{ .root.Values.api.rateLimit.writeRequests | quote }}
- name: CORPUSKIT_API_RATE_LIMIT_RETENTION_WINDOWS
  value: {{ .root.Values.api.rateLimit.retentionWindows | quote }}
{{- end -}}

{{- define "corpuskit.tmpVolume" -}}
- name: tmp
  emptyDir:
    medium: Memory
    sizeLimit: {{ . | quote }}
{{- end -}}

{{- define "corpuskit.espeakTmpVolume" -}}
# phonemizer copies libespeak per wrapper before dlopen(); scope executable
# temporary storage to this small in-memory volume instead of general /tmp.
- name: espeak-tmp
  emptyDir:
    medium: Memory
    sizeLimit: "64Mi"
{{- end -}}

{{- define "corpuskit.commonPodMetadata" -}}
{{- with .Values.global.podAnnotations }}
annotations:
{{ toYaml . | indent 2 }}
{{- end }}
labels:
  {{- include "corpuskit.labels" . | nindent 2 }}
  {{- with .Values.global.podLabels }}
  {{- toYaml . | nindent 2 }}
  {{- end }}
{{- end -}}
