"""Fail-closed deployment contract for the production Helm chart."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "corpuskit"
VALUES = CHART / "ci" / "production-values.yaml"
NAMESPACE = "corpuskit-prod"
DEPLOYMENT_WORKFLOW = ROOT / ".github" / "workflows" / "deployment.yml"
KUBERNETES_SCHEMA_VERSION = "1.35.0"
HELM_VERSION = "4.2.3"
HELM_LINUX_AMD64_SHA256 = "e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c"
KUBECONFORM_VERSION = "0.8.0"
KUBECONFORM_LINUX_AMD64_SHA256 = "9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883"
UV_VERSION = "0.12.3"
ESPEAK_TMPDIR = "/run/corpuskit-espeak"
XDG_CONFIG_HOME = "/tmp/corpuskit-xdg"  # noqa: S108 - asserted container path


def _helm() -> str:
    configured = os.environ.get("HELM_BIN")
    candidates = [
        configured,
        shutil.which("helm"),
        str(ROOT / ".tmp-deploy-tools" / "windows-amd64" / "helm.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    if os.environ.get("CORPUSKIT_REQUIRE_HELM") == "1":
        pytest.fail("Helm is required but HELM_BIN does not identify an executable")
    pytest.skip("Helm is validated in the deployment workflow")


def _run_helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the executable and every argument are test-owned
        [_helm(), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    result = _run_helm(
        "template",
        "acceptance",
        str(CHART),
        "--namespace",
        NAMESPACE,
        "--include-crds",
        "-f",
        str(VALUES),
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _objects(rendered: list[dict[str, Any]], kind: str) -> Iterator[dict[str, Any]]:
    return (item for item in rendered if item.get("kind") == kind)


def _workload_pods(rendered: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    for item in rendered:
        kind = item.get("kind")
        if kind == "CronJob":
            yield item["metadata"]["name"], item["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        elif kind in {"Deployment", "Job"}:
            yield item["metadata"]["name"], item["spec"]["template"]["spec"]


def _deployment(rendered: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
    return next(
        item
        for item in _objects(rendered, "Deployment")
        if item["metadata"]["name"].endswith(suffix)
    )


def _network_policy(rendered: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
    return next(
        item
        for item in _objects(rendered, "NetworkPolicy")
        if item["metadata"]["name"].endswith(suffix)
    )


def _egress_ports_for_cidr(policy: dict[str, Any], cidr: str) -> set[int]:
    return {
        port["port"]
        for rule in policy["spec"].get("egress", [])
        if any(peer.get("ipBlock", {}).get("cidr") == cidr for peer in rule.get("to", []))
        for port in rule.get("ports", [])
    }


def _secret_env(workload: dict[str, Any]) -> set[str]:
    return _pod_secret_env(workload["spec"]["template"]["spec"])


def _pod_secret_env(pod: dict[str, Any]) -> set[str]:
    container = pod["containers"][0]
    return {
        item["name"]
        for item in container.get("env", [])
        if "secretKeyRef" in item.get("valueFrom", {})
    }


def _pod_plain_env(pod: dict[str, Any]) -> dict[str, str]:
    return {
        item["name"]: item["value"]
        for item in pod["containers"][0].get("env", [])
        if "value" in item
    }


def _render_override(
    tmp_path: Path,
    override: dict[str, Any],
    *,
    skip_schema_validation: bool = False,
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "override.yaml"
    path.write_text(yaml.safe_dump(override, sort_keys=True), encoding="utf-8")
    args = [
        "template",
        "negative",
        str(CHART),
        "--namespace",
        NAMESPACE,
        "-f",
        str(VALUES),
        "-f",
        str(path),
    ]
    if skip_schema_validation:
        args.append("--skip-schema-validation")
    return _run_helm(*args)


def test_values_schema_has_no_open_object_escape_hatches() -> None:
    schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))

    def visit(value: object, path: str) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert "additionalProperties" in value, path
            assert value.get("additionalProperties") is not True, path
            for key, child in value.items():
                visit(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")

    visit(schema, "#")


def test_deployment_toolchain_and_kubernetes_floor_are_pinned() -> None:
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
    workflow = DEPLOYMENT_WORKFLOW.read_text(encoding="utf-8")
    chart_readme = (CHART / "README.md").read_text(encoding="utf-8")
    production_runbook = (ROOT / "docs" / "operations" / "kubernetes-production.md").read_text(
        encoding="utf-8"
    )

    assert chart["kubeVersion"] == f">={KUBERNETES_SCHEMA_VERSION}-0"
    assert f'HELM_VERSION: "{HELM_VERSION}"' in workflow
    assert f"HELM_SHA256: {HELM_LINUX_AMD64_SHA256}" in workflow
    assert f'KUBECONFORM_VERSION: "{KUBECONFORM_VERSION}"' in workflow
    assert f"KUBECONFORM_SHA256: {KUBECONFORM_LINUX_AMD64_SHA256}" in workflow
    assert f'UV_VERSION: "{UV_VERSION}"' in workflow
    assert f"-kubernetes-version {KUBERNETES_SCHEMA_VERSION}" in workflow
    assert f"Helm {HELM_VERSION} and kubeconform {KUBECONFORM_VERSION}" in chart_readme
    assert f"-kubernetes-version {KUBERNETES_SCHEMA_VERSION}" in chart_readme
    assert "Kubernetes 1.35 or newer" in production_runbook
    assert f"Helm {HELM_VERSION} and kubeconform {KUBECONFORM_VERSION}" in production_runbook


def test_browser_return_allowlist_exactly_matches_mounted_pages() -> None:
    defaults = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    app_root = ROOT / "apps" / "web" / "src" / "app"
    actual = {"/"}
    actual.update(
        f"/{page.parent.relative_to(app_root).as_posix()}"
        for page in app_root.rglob("page.tsx")
        if page.parent != app_root
    )

    assert defaults["web"]["allowedReturnPaths"] == [
        "/",
        "/projects",
        "/evaluate",
        "/analysis",
        "/capabilities",
        "/g2p",
        "/inventory",
        "/coverage",
        "/selection",
        "/generation",
        "/advanced",
        "/jobs",
        "/artifacts",
    ]
    assert set(defaults["web"]["allowedReturnPaths"]) == actual


def test_chart_lints_strictly() -> None:
    result = _run_helm("lint", str(CHART), "--strict", "-f", str(VALUES))
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_expected_components_render_without_bundled_dependencies(
    rendered: list[dict[str, Any]],
) -> None:
    names = {item["metadata"]["name"] for item in _objects(rendered, "Deployment")}
    assert {name.rsplit("-", 1)[-1] for name in names}  # sanity-check nonempty names
    assert {
        "acceptance-corpuskit-api",
        "acceptance-corpuskit-web",
        "acceptance-corpuskit-dispatcher",
        "acceptance-corpuskit-worker-batch",
        "acceptance-corpuskit-worker-external-provider",
        "acceptance-corpuskit-worker-gpu-inference",
        "acceptance-corpuskit-worker-gpu-training",
        "acceptance-corpuskit-telemetry",
    } == names
    assert not any(
        token in item["metadata"]["name"]
        for item in rendered
        if item.get("kind") in {"Deployment", "StatefulSet"}
        for token in ("postgres", "redis", "minio", "temporal-server")
    )


def test_every_pod_and_container_uses_restricted_security(rendered: list[dict[str, Any]]) -> None:
    for name, pod in _workload_pods(rendered):
        assert pod["serviceAccountName"] != "default", name
        assert pod["automountServiceAccountToken"] is False, name
        assert pod["enableServiceLinks"] is False, name
        assert pod["nodeSelector"]["kubernetes.io/os"] == "linux", name
        assert pod["nodeSelector"]["kubernetes.io/arch"] == "amd64", name
        assert pod["securityContext"]["runAsNonRoot"] is True, name
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault", name
        for container in [*pod.get("initContainers", []), *pod["containers"]]:
            security = container["securityContext"]
            assert security["allowPrivilegeEscalation"] is False, name
            assert security["readOnlyRootFilesystem"] is True, name
            assert security["runAsNonRoot"] is True, name
            assert security["capabilities"]["drop"] == ["ALL"], name
            assert "@sha256:" in container["image"], name
            assert container["resources"]["requests"], name
            assert container["resources"]["limits"], name


def test_espeak_capable_pods_use_a_bounded_dedicated_tmpdir(
    rendered: list[dict[str, Any]],
) -> None:
    suffixes = (
        "-api",
        "-worker-batch",
        "-worker-external-provider",
        "-worker-gpu-inference",
        "-worker-gpu-training",
    )
    for suffix in suffixes:
        pod = _deployment(rendered, suffix)["spec"]["template"]["spec"]
        container = pod["containers"][0]
        environment = _pod_plain_env(pod)
        mounts = {item["name"]: item for item in container["volumeMounts"]}
        volumes = {item["name"]: item for item in pod["volumes"]}

        assert environment["TMPDIR"] == ESPEAK_TMPDIR, suffix
        assert environment["XDG_CONFIG_HOME"] == XDG_CONFIG_HOME, suffix
        assert mounts["espeak-tmp"] == {
            "name": "espeak-tmp",
            "mountPath": ESPEAK_TMPDIR,
        }, suffix
        assert volumes["espeak-tmp"] == {
            "name": "espeak-tmp",
            "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"},
        }, suffix

    excluded = ("-web", "-dispatcher", "-telemetry")
    for suffix in excluded:
        pod = _deployment(rendered, suffix)["spec"]["template"]["spec"]
        assert "TMPDIR" not in _pod_plain_env(pod), suffix
        assert "XDG_CONFIG_HOME" not in _pod_plain_env(pod), suffix
        assert all(
            item["mountPath"] != ESPEAK_TMPDIR
            for item in pod["containers"][0].get("volumeMounts", [])
        ), suffix


def test_secret_exposure_is_exactly_role_scoped(rendered: list[dict[str, Any]]) -> None:
    kms = {"CORPUSKIT_ARTIFACT_S3_KMS_KEY_ID"}
    database = {"CORPUSKIT_DATABASE_URL"}
    temporal = {"CORPUSKIT_TEMPORAL_API_KEY"}
    object_store = {
        "CORPUSKIT_ARTIFACT_S3_ACCESS_KEY_ID",
        "CORPUSKIT_ARTIFACT_S3_SECRET_ACCESS_KEY",
        *kms,
    }
    adoption = {"CORPUSKIT_ADOPTION_DATABASE_URL"}
    assert _secret_env(_deployment(rendered, "-api")) == {
        *database,
        *temporal,
        *object_store,
        "CORPUSKIT_METRICS_BEARER_TOKEN",
    }
    assert _secret_env(_deployment(rendered, "-dispatcher")) == database | temporal
    for suffix in ("-worker-batch", "-worker-gpu-inference", "-worker-gpu-training"):
        assert _secret_env(_deployment(rendered, suffix)) == (
            database | adoption | temporal | object_store
        )
    assert _secret_env(_deployment(rendered, "-worker-external-provider")) == (
        database
        | adoption
        | temporal
        | object_store
        | {
            "CORPUSKIT_PROVIDER_OPENAI_API_KEY",
            "CORPUSKIT_PROVIDER_PROMPT_COVERAGE_V1",
        }
    )
    assert _secret_env(_deployment(rendered, "-web")) == {
        "CORPUSKIT_WEB_OIDC_CLIENT_SECRET",
        "CORPUSKIT_WEB_SESSION_REDIS_URL",
        "CORPUSKIT_WEB_STATE_SECRET",
        "CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS",
    }
    assert _secret_env(_deployment(rendered, "-telemetry")) == {"OTEL_EXPORTER_OTLP_AUTHORIZATION"}
    maintenance = next(_objects(rendered, "CronJob"))
    maintenance_pod = maintenance["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert _pod_secret_env(maintenance_pod) == database | object_store
    migration = next(
        item for item in _objects(rendered, "Job") if item["metadata"]["name"].endswith("-migrate")
    )
    phoible = next(
        item for item in _objects(rendered, "Job") if item["metadata"]["name"].endswith("-phoible")
    )
    assert _pod_secret_env(migration["spec"]["template"]["spec"]) == database
    assert _pod_secret_env(phoible["spec"]["template"]["spec"]) == set()
    telemetry_volumes = _deployment(rendered, "-telemetry")["spec"]["template"]["spec"]["volumes"]
    assert {
        volume["secret"]["secretName"] for volume in telemetry_volumes if "secret" in volume
    } == {"corpuskit-test-metrics"}
    assert not list(_objects(rendered, "Secret"))


def test_rate_limit_window_contract_is_identical_for_api_and_maintenance(
    rendered: list[dict[str, Any]],
) -> None:
    api_env = _pod_plain_env(_deployment(rendered, "-api")["spec"]["template"]["spec"])
    maintenance = next(_objects(rendered, "CronJob"))
    maintenance_env = _pod_plain_env(maintenance["spec"]["jobTemplate"]["spec"]["template"]["spec"])
    shared = {
        "CORPUSKIT_API_RATE_LIMIT_WINDOW_SECONDS",
        "CORPUSKIT_API_RATE_LIMIT_READ_REQUESTS",
        "CORPUSKIT_API_RATE_LIMIT_WRITE_REQUESTS",
        "CORPUSKIT_API_RATE_LIMIT_RETENTION_WINDOWS",
    }
    assert {name: api_env[name] for name in shared} == {
        name: maintenance_env[name] for name in shared
    }
    assert api_env["CORPUSKIT_API_RATE_LIMIT_ENABLED"] == "true"
    assert "CORPUSKIT_API_RATE_LIMIT_ENABLED" not in maintenance_env

    for suffix in (
        "-web",
        "-dispatcher",
        "-worker-batch",
        "-worker-external-provider",
        "-worker-gpu-inference",
        "-worker-gpu-training",
        "-telemetry",
    ):
        env = _pod_plain_env(_deployment(rendered, suffix)["spec"]["template"]["spec"])
        assert not shared & env.keys(), suffix


def test_advanced_policy_env_is_redacted_and_exactly_role_scoped(
    rendered: list[dict[str, Any]],
) -> None:
    fixture = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    profiles = fixture["workers"]
    policy_env_names = {
        "CORPUSKIT_WORKER_HOSTED_MODEL_POLICIES",
        "CORPUSKIT_WORKER_HUGGINGFACE_REPOSITORY_POLICIES",
        "CORPUSKIT_WORKER_LOCAL_MODEL_POLICIES",
        "CORPUSKIT_WORKER_DATG_RUNTIME_POLICIES",
        "CORPUSKIT_WORKER_PHON_RL_RUNTIME_POLICIES",
    }

    api_env = _pod_plain_env(_deployment(rendered, "-api")["spec"]["template"]["spec"])
    assert policy_env_names <= api_env.keys()
    assert (
        json.loads(api_env["CORPUSKIT_WORKER_HOSTED_MODEL_POLICIES"])
        == profiles["externalProvider"]["hostedModelPolicies"]
    )
    assert (
        json.loads(api_env["CORPUSKIT_WORKER_HUGGINGFACE_REPOSITORY_POLICIES"])
        == (profiles["externalProvider"]["huggingFaceRepositoryPolicies"])
    )
    assert (
        json.loads(api_env["CORPUSKIT_WORKER_LOCAL_MODEL_POLICIES"])
        == profiles["gpuInference"]["localModelPolicies"]
    )
    assert profiles["gpuInference"]["localModelPolicies"][0]["allow_phon_rl_adapters"] is True
    assert (
        json.loads(api_env["CORPUSKIT_WORKER_DATG_RUNTIME_POLICIES"])
        == profiles["gpuInference"]["datgRuntimePolicies"]
    )
    assert (
        json.loads(api_env["CORPUSKIT_WORKER_PHON_RL_RUNTIME_POLICIES"])
        == profiles["gpuTraining"]["phonRlRuntimePolicies"]
    )
    assert {
        name: value
        for name, value in api_env.items()
        if "CACHE" in name and name.startswith("CORPUSKIT_WORKER_")
    } == {
        "CORPUSKIT_WORKER_DATG_INDEX_CACHE_ROOT": "/datg-indexes",
        "CORPUSKIT_WORKER_DATG_CACHE_MOUNT_READ_ONLY": "true",
    }

    expected_by_suffix = {
        "-worker-batch": {"CORPUSKIT_WORKER_DATG_RUNTIME_POLICIES"},
        "-worker-external-provider": {
            "CORPUSKIT_WORKER_HOSTED_MODEL_POLICIES",
            "CORPUSKIT_WORKER_HUGGINGFACE_REPOSITORY_POLICIES",
        },
        "-worker-gpu-inference": {
            "CORPUSKIT_WORKER_LOCAL_MODEL_POLICIES",
            "CORPUSKIT_WORKER_DATG_RUNTIME_POLICIES",
        },
        "-worker-gpu-training": {"CORPUSKIT_WORKER_PHON_RL_RUNTIME_POLICIES"},
    }
    for suffix, expected in expected_by_suffix.items():
        env = _pod_plain_env(_deployment(rendered, suffix)["spec"]["template"]["spec"])
        assert policy_env_names & env.keys() == expected
    batch_env = _pod_plain_env(_deployment(rendered, "-worker-batch")["spec"]["template"]["spec"])
    gpu_env = _pod_plain_env(
        _deployment(rendered, "-worker-gpu-inference")["spec"]["template"]["spec"]
    )
    assert json.loads(batch_env["CORPUSKIT_WORKER_DATG_RUNTIME_POLICIES"]) == json.loads(
        gpu_env["CORPUSKIT_WORKER_DATG_RUNTIME_POLICIES"]
    )
    assert json.loads(api_env["CORPUSKIT_WORKER_DATG_RUNTIME_POLICIES"]) == json.loads(
        gpu_env["CORPUSKIT_WORKER_DATG_RUNTIME_POLICIES"]
    )

    for suffix in ("-web", "-dispatcher", "-telemetry"):
        env = _pod_plain_env(_deployment(rendered, suffix)["spec"]["template"]["spec"])
        assert not policy_env_names & env.keys()
    maintenance = next(_objects(rendered, "CronJob"))
    maintenance_pod = maintenance["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert not policy_env_names & _pod_plain_env(maintenance_pod).keys()
    for job in _objects(rendered, "Job"):
        assert not policy_env_names & _pod_plain_env(job["spec"]["template"]["spec"]).keys()


def test_datg_publication_cache_is_role_scoped_and_shared(
    rendered: list[dict[str, Any]],
) -> None:
    expected_claim = "corpuskit-test-datg-cache"

    def datg_mount(suffix: str) -> tuple[dict[str, Any], dict[str, Any]]:
        pod = _deployment(rendered, suffix)["spec"]["template"]["spec"]
        container = pod["containers"][0]
        mount = next(
            item for item in container["volumeMounts"] if item["name"] == "datg-index-cache"
        )
        volume = next(item for item in pod["volumes"] if item["name"] == "datg-index-cache")
        return mount, volume

    api_mount, api_volume = datg_mount("-api")
    batch_mount, batch_volume = datg_mount("-worker-batch")
    gpu_mount, gpu_volume = datg_mount("-worker-gpu-inference")
    assert api_mount == {"name": "datg-index-cache", "mountPath": "/datg-indexes", "readOnly": True}
    assert batch_mount == {
        "name": "datg-index-cache",
        "mountPath": "/datg-index-publish",
        "readOnly": False,
    }
    assert gpu_mount == {"name": "datg-index-cache", "mountPath": "/datg-indexes", "readOnly": True}
    for volume in (api_volume, batch_volume, gpu_volume):
        assert volume["persistentVolumeClaim"]["claimName"] == expected_claim

    batch_env = _pod_plain_env(_deployment(rendered, "-worker-batch")["spec"]["template"]["spec"])
    assert batch_env["CORPUSKIT_WORKER_DATG_INDEX_PUBLISH_ROOT"] == "/datg-index-publish"
    for suffix in ("-api", "-worker-gpu-inference"):
        env = _pod_plain_env(_deployment(rendered, suffix)["spec"]["template"]["spec"])
        assert env["CORPUSKIT_WORKER_DATG_INDEX_CACHE_ROOT"] == "/datg-indexes"
        assert env["CORPUSKIT_WORKER_DATG_CACHE_MOUNT_READ_ONLY"] == "true"

    for suffix in ("-web", "-dispatcher", "-worker-external-provider", "-worker-gpu-training"):
        pod = _deployment(rendered, suffix)["spec"]["template"]["spec"]
        assert all(item["name"] != "datg-index-cache" for item in pod.get("volumes", []))


def test_api_rollout_checksum_tracks_advanced_policy_changes(
    rendered: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    fixture = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    profile = deepcopy(fixture["workers"]["externalProvider"])
    profile["huggingFaceRepositoryPolicies"][0]["max_samples"] = 999
    result = _render_override(tmp_path, {"workers": {"externalProvider": profile}})
    assert result.returncode == 0, result.stderr
    changed = [document for document in yaml.safe_load_all(result.stdout) if document]

    baseline_checksum = _deployment(rendered, "-api")["spec"]["template"]["metadata"][
        "annotations"
    ]["checksum/config"]
    changed_checksum = _deployment(changed, "-api")["spec"]["template"]["metadata"]["annotations"][
        "checksum/config"
    ]
    assert baseline_checksum != changed_checksum


def test_datg_build_and_generation_runtime_policies_must_match(tmp_path: Path) -> None:
    fixture = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    profile = deepcopy(fixture["workers"]["batch"])
    profile["datgRuntimePolicies"][0]["runtime_id"] = "datg-build-only"
    result = _render_override(tmp_path, {"workers": {"batch": profile}})
    assert result.returncode != 0
    assert "must be identical" in result.stderr


def test_database_temporal_and_service_identities_are_distinct(
    rendered: list[dict[str, Any]],
) -> None:
    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    defaults = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert (
        len({item["secretName"] for item in values["database"].values() if isinstance(item, dict)})
        == 12
    )
    assert len({item["secretName"] for item in values["temporal"]["credentials"].values()}) == 6
    assert len(set(defaults["serviceAccounts"].values())) == 11
    assert len({item["metadata"]["name"] for item in _objects(rendered, "ServiceAccount")}) == 11


def test_gpu_profiles_have_exact_resources_and_placement(rendered: list[dict[str, Any]]) -> None:
    for suffix in ("-worker-gpu-inference", "-worker-gpu-training"):
        pod = _deployment(rendered, suffix)["spec"]["template"]["spec"]
        resources = pod["containers"][0]["resources"]
        assert resources["requests"]["nvidia.com/gpu"] == resources["limits"]["nvidia.com/gpu"]
        assert int(resources["limits"]["nvidia.com/gpu"]) >= 1
        assert pod["nodeSelector"]["kubernetes.io/arch"] == "amd64"
        assert pod["nodeSelector"]["corpuskit.io/gpu-class"]
        assert any(item["key"] == "nvidia.com/gpu" for item in pod["tolerations"])
    for suffix in ("-worker-batch", "-worker-external-provider"):
        resources = _deployment(rendered, suffix)["spec"]["template"]["spec"]["containers"][0][
            "resources"
        ]
        assert "nvidia.com/gpu" not in resources["requests"]
        assert "nvidia.com/gpu" not in resources["limits"]


def test_network_policies_default_deny_and_isolate_provider_egress(
    rendered: list[dict[str, Any]],
) -> None:
    policies = list(_objects(rendered, "NetworkPolicy"))
    default = next(item for item in policies if item["metadata"]["name"].endswith("default-deny"))
    assert default["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}

    provider_cidr = "192.0.2.60/32"
    permitting = []
    for item in policies:
        cidrs = {
            peer["ipBlock"]["cidr"]
            for rule in item["spec"].get("egress", [])
            for peer in rule.get("to", [])
            if "ipBlock" in peer
        }
        if provider_cidr in cidrs:
            permitting.append(item["metadata"]["name"])
    assert permitting == ["acceptance-corpuskit-worker-external-provider-egress"]


def test_chart_owned_metadata_is_fixed_and_safe_custom_metadata_is_preserved(
    tmp_path: Path,
) -> None:
    result = _render_override(
        tmp_path,
        {
            "global": {
                "podLabels": {"example.com/owner": "platform"},
                "podAnnotations": {"example.com/change-ticket": "CHG-1234"},
            },
            "ingress": {
                "annotations": {"external-dns.alpha.kubernetes.io/hostname": "corpuskit.example"}
            },
        },
    )
    assert result.returncode == 0, result.stderr
    documents = [document for document in yaml.safe_load_all(result.stdout) if document]

    ingress = next(_objects(documents, "Ingress"))
    assert ingress["metadata"]["annotations"] == {
        "external-dns.alpha.kubernetes.io/hostname": "corpuskit.example",
        "nginx.ingress.kubernetes.io/force-ssl-redirect": "true",
        "nginx.ingress.kubernetes.io/proxy-body-size": "10m",
        "nginx.ingress.kubernetes.io/proxy-read-timeout": "60",
        "nginx.ingress.kubernetes.io/ssl-redirect": "true",
    }

    for suffix in (
        "-api",
        "-web",
        "-dispatcher",
        "-worker-batch",
        "-worker-external-provider",
        "-worker-gpu-inference",
        "-worker-gpu-training",
    ):
        deployment = _deployment(documents, suffix)
        pod_metadata = deployment["spec"]["template"]["metadata"]
        selector = deployment["spec"]["selector"]["matchLabels"]
        assert selector.items() <= pod_metadata["labels"].items()
        assert pod_metadata["labels"]["example.com/owner"] == "platform"
        assert pod_metadata["annotations"]["example.com/change-ticket"] == "CHG-1234"
        assert any(key.startswith("checksum/") for key in pod_metadata["annotations"])


def test_runtime_endpoint_ports_match_network_policy_egress(tmp_path: Path) -> None:
    result = _render_override(
        tmp_path,
        {
            "oidc": {
                "issuer": "https://identity.example:443/realms/corpuskit",
                "redirectUri": "https://corpuskit.example:443/auth/callback",
            },
            "temporal": {"address": "temporal.example:8233", "port": 8233},
            "artifactStorage": {"endpoint": "https://objects.example:443"},
            "monitoring": {
                "otelCollector": {"exporterEndpoint": "https://telemetry.example:443/v1/otlp"}
            },
        },
    )
    assert result.returncode == 0, result.stderr
    documents = [document for document in yaml.safe_load_all(result.stdout) if document]

    api_egress = _network_policy(documents, "-api-egress")
    assert _egress_ports_for_cidr(api_egress, "192.0.2.20/32") == {8233}
    assert _egress_ports_for_cidr(api_egress, "192.0.2.30/32") == {443}
    assert _egress_ports_for_cidr(api_egress, "192.0.2.50/32") == {443}
    telemetry_egress = _network_policy(documents, "-telemetry")
    assert _egress_ports_for_cidr(telemetry_egress, "192.0.2.70/32") == {443}


def test_monitoring_contract_contains_rules_dashboard_and_protected_scrape(
    rendered: list[dict[str, Any]],
) -> None:
    monitor = next(_objects(rendered, "ServiceMonitor"))
    endpoint = monitor["spec"]["endpoints"][0]
    assert endpoint["path"] == "/internal/metrics"
    assert endpoint["authorization"]["credentials"]["name"] == "corpuskit-test-metrics"
    rules = next(_objects(rendered, "PrometheusRule"))
    alerts = {
        rule["alert"]
        for group in rules["spec"]["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }
    assert {
        "CorpusKitApiScrapeMissing",
        "CorpusKitOutboxLagHigh",
        "CorpusKitWorkflowTelemetryMissing",
        "CorpusKitDependencyNotReady",
        "CorpusKitProviderDailyBudgetHigh",
    } <= alerts
    dashboard = next(
        item
        for item in _objects(rendered, "ConfigMap")
        if item["metadata"]["name"].endswith("grafana-dashboard")
    )
    json.loads(dashboard["data"]["corpuskit-overview.json"])


@pytest.mark.parametrize(
    "override",
    [
        {"unknownProductionKey": True},
        {"images": {"api": {"digest": ""}}},
        {"images": {"api": {"digest": "sha256:" + "0" * 64}}},
        {"temporal": {"tls": False}},
        {"networkPolicy": {"enabled": False}},
        {"networkPolicy": {"cidrs": {"database": []}}},
        {"artifactStorage": {"endpoint": "http://objects.example"}},
        {"artifactStorage": {"endpoint": "https://objects.example:8443"}},
        {"oidc": {"issuer": "https://identity.example:8443/realms/corpuskit"}},
        {"oidc": {"redirectUri": "https://corpuskit.example:8443/auth/callback"}},
        {
            "monitoring": {
                "otelCollector": {"exporterEndpoint": "https://telemetry.example:8443/v1/otlp"}
            }
        },
        {"temporal": {"address": "temporal.example:8234"}},
        {"ingress": {"annotations": {"nginx.ingress.kubernetes.io/ssl-redirect": "false"}}},
        {"global": {"podLabels": {"app.kubernetes.io/component": "attacker"}}},
        {"global": {"podLabels": {"corpuskit.io/worker-profile": "attacker"}}},
        {"global": {"podAnnotations": {"checksum/config": "attacker"}}},
        {"global": {"podAnnotations": {"checksum/runtime-policy": "attacker"}}},
        {"global": {"podAnnotations": {"helm.sh/hook": "post-install"}}},
        {"global": {"nodeSelector": {"kubernetes.io/arch": "arm64"}}},
        {"global": {"nodeSelector": {"kubernetes.io/os": "windows"}}},
        {"workers": {"gpuInference": {"nodeSelector": {"kubernetes.io/arch": "amd64"}}}},
        {"workers": {"externalProvider": {"hostedModelPolicies": []}}},
        {"workers": {"externalProvider": {"huggingFaceRepositoryPolicies": []}}},
        {"workers": {"gpuInference": {"localModelPolicies": []}}},
        {"workers": {"gpuInference": {"datgRuntimePolicies": []}}},
        {"workers": {"gpuTraining": {"phonRlRuntimePolicies": []}}},
        {"workers": {"gpuInference": {"resources": {"requests": {"nvidia.com/gpu": "2"}}}}},
        {"web": {"allowedReturnPaths": ["/", "/../escape"]}},
    ],
)
def test_insecure_or_incomplete_values_fail_render(
    tmp_path: Path,
    override: dict[str, Any],
) -> None:
    assert _render_override(tmp_path, override).returncode != 0


@pytest.mark.parametrize(
    "override",
    [
        {"ingress": {"annotations": {"nginx.ingress.kubernetes.io/ssl-redirect": "false"}}},
        {"global": {"podLabels": {"app.kubernetes.io/component": "attacker"}}},
        {"global": {"podAnnotations": {"checksum/config": "attacker"}}},
        {"global": {"nodeSelector": {"kubernetes.io/arch": "arm64"}}},
        {"artifactStorage": {"endpoint": "https://objects.example:8443"}},
        {"oidc": {"issuer": "https://identity.example:8443/realms/corpuskit"}},
        {
            "monitoring": {
                "otelCollector": {"exporterEndpoint": "https://telemetry.example:8443/v1/otlp"}
            }
        },
        {"temporal": {"address": "temporal.example:8234"}},
        {"workers": {"gpuInference": {"nodeSelector": {"kubernetes.io/arch": "amd64"}}}},
    ],
)
def test_template_guards_fail_closed_when_schema_validation_is_skipped(
    tmp_path: Path,
    override: dict[str, Any],
) -> None:
    result = _render_override(tmp_path, override, skip_schema_validation=True)
    assert result.returncode != 0


def test_duplicate_database_and_temporal_secrets_fail_render(tmp_path: Path) -> None:
    fixture = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    db = deepcopy(fixture["database"])
    db["dispatcher"]["secretName"] = db["api"]["secretName"]
    assert _render_override(tmp_path, {"database": db}).returncode != 0
    temporal = deepcopy(fixture["temporal"]["credentials"])
    temporal["dispatcher"]["secretName"] = temporal["api"]["secretName"]
    assert _render_override(tmp_path, {"temporal": {"credentials": temporal}}).returncode != 0


def test_provider_secret_policy_mapping_is_an_exact_bijection(tmp_path: Path) -> None:
    fixture = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    profile = deepcopy(fixture["workers"]["externalProvider"])
    profile["hostedModelPolicies"][0]["model"] = "anthropic/demo-model"
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0

    profile = deepcopy(fixture["workers"]["externalProvider"])
    profile["hostedModelPolicies"][0]["request_delay_seconds"] = 30.01
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0

    profile = deepcopy(fixture["workers"]["externalProvider"])
    profile["providerSecrets"].append(deepcopy(profile["providerSecrets"][0]))
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0

    profile = deepcopy(fixture["workers"]["externalProvider"])
    profile["hostedModelPolicies"][0]["prompt_templates"][0]["template_ref"]["reference"] = (
        "secret://env/CORPUSKIT_PROVIDER_MISSING_PROMPT"
    )
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0

    profile = deepcopy(fixture["workers"]["externalProvider"])
    prompt = profile["hostedModelPolicies"][0]["prompt_templates"][0]
    prompt["max_rendered_bytes"] = prompt["size_bytes"] - 1
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0

    profile = deepcopy(fixture["workers"]["externalProvider"])
    duplicate_prompt = deepcopy(profile["hostedModelPolicies"][0]["prompt_templates"][0])
    profile["hostedModelPolicies"][0]["prompt_templates"].append(duplicate_prompt)
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0

    profile = deepcopy(fixture["workers"]["externalProvider"])
    profile["hostedModelPolicies"][0]["credential_ref"]["reference"] = (
        "secret://env/CORPUSKIT_PROVIDER_MISSING"
    )
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0

    profile = deepcopy(fixture["workers"]["externalProvider"])
    profile["providerSecrets"].append(
        {
            "name": "CORPUSKIT_PROVIDER_ORPHAN",
            "secretName": "corpuskit-test-provider-orphan",
            "key": "api-key",
        }
    )
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0


def test_hugging_face_repository_policies_are_immutable_and_unique(tmp_path: Path) -> None:
    fixture = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    profile = deepcopy(fixture["workers"]["externalProvider"])
    duplicate_selector = deepcopy(profile["huggingFaceRepositoryPolicies"][0])
    duplicate_selector["max_samples"] = 999
    profile["huggingFaceRepositoryPolicies"].append(duplicate_selector)
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0

    profile = deepcopy(fixture["workers"]["externalProvider"])
    profile["huggingFaceRepositoryPolicies"][0]["trust_remote_code"] = True
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0

    profile = deepcopy(fixture["workers"]["externalProvider"])
    profile["huggingFaceRepositoryPolicies"][0]["revision"] = "main"
    assert _render_override(tmp_path, {"workers": {"externalProvider": profile}}).returncode != 0
