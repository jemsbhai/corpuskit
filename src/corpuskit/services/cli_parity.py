"""Build safe, exact CorpusGen CLI previews without executing a shell."""

from __future__ import annotations

import json
import shlex

from corpuskit.domain.cli_parity import (
    CliCommandPreview,
    CliEvaluateRequest,
    CliGenerateRequest,
    CliGenerationBackend,
    CliGuidance,
    CliInventoryRequest,
    CliPreviewRequest,
    CliReproducibility,
    CliSelectRequest,
    CliTargetMode,
)

_UTF8_WARNING = (
    "The preview enables Python UTF-8 mode because IPA output may not be representable "
    "by a platform's legacy console encoding."
)


class CliParityService:
    """Serialize validated options into argv and shell-safe display forms."""

    def preview(self, request: CliPreviewRequest) -> CliCommandPreview:
        if isinstance(request, CliInventoryRequest):
            argv, reproducibility, warnings = self._inventory(request)
        elif isinstance(request, CliEvaluateRequest):
            argv, reproducibility, warnings = self._evaluate(request)
        elif isinstance(request, CliSelectRequest):
            argv, reproducibility, warnings = self._select(request)
        else:
            argv, reproducibility, warnings = self._generate(request)
        return CliCommandPreview(
            workflow=request.workflow,
            argv=tuple(argv),
            posix_command=f"PYTHONUTF8=1 {shlex.join(argv)}",
            powershell_command=(
                "$env:PYTHONUTF8 = '1'; & "
                + " ".join(self._powershell_quote(argument) for argument in argv)
            ),
            reproducibility=reproducibility,
            warnings=(_UTF8_WARNING, *warnings),
        )

    @staticmethod
    def _inventory(
        request: CliInventoryRequest,
    ) -> tuple[list[str], CliReproducibility, tuple[str, ...]]:
        argv = [
            "corpusgen",
            "inventory",
            "--language",
            request.language,
            "--format",
            request.output_format.value,
        ]
        if request.source is not None:
            argv.extend(("--source", request.source))
        return (
            argv,
            CliReproducibility.EXACT_INPUTS_REQUIRED,
            ("The result depends on the installed, checksum-verified PHOIBLE snapshot.",),
        )

    @staticmethod
    def _evaluate(
        request: CliEvaluateRequest,
    ) -> tuple[list[str], CliReproducibility, tuple[str, ...]]:
        argv = ["corpusgen", "evaluate"]
        if request.file_path is not None:
            argv.extend(("--file", request.file_path))
        else:
            argv.extend(request.sentences)
        argv.extend(("--language", request.language))
        if request.target is CliTargetMode.PHOIBLE:
            argv.extend(("--target", "phoible"))
        argv.extend(
            (
                "--unit",
                request.unit.value,
                "--format",
                request.output_format.value,
                "--verbosity",
                request.verbosity.value,
            )
        )
        return (
            argv,
            CliReproducibility.EXACT_INPUTS_REQUIRED,
            (
                "Derived targets come from the supplied corpus; PHOIBLE targets also depend "
                "on the installed snapshot.",
                "The CLI does not accept an arbitrary explicit target list; use the CorpusKit "
                "evaluation API for that workflow.",
            ),
        )

    @staticmethod
    def _select(
        request: CliSelectRequest,
    ) -> tuple[list[str], CliReproducibility, tuple[str, ...]]:
        argv = [
            "corpusgen",
            "select",
            "--file",
            request.file_path,
            "--language",
            request.language,
            "--unit",
            request.unit.value,
            "--algorithm",
            request.algorithm.value,
            "--target-coverage",
            str(request.target_coverage),
            "--format",
            request.output_format.value,
        ]
        if request.target is CliTargetMode.PHOIBLE:
            argv.extend(("--target", "phoible"))
        if request.target_distribution:
            distribution = {item.unit: item.weight for item in request.target_distribution}
            argv.extend(
                (
                    "--target-distribution",
                    json.dumps(
                        distribution,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
        if request.max_sentences is not None:
            argv.extend(("--max-sentences", str(request.max_sentences)))
        if request.output_path is not None:
            argv.extend(("--output", request.output_path))
        stochastic = request.algorithm.value in {"stochastic", "nsga2"}
        warnings = (
            "The CLI omits API-only controls such as stochastic seeds, ILP timeouts, "
            "NSGA-II population settings, and general unit weights.",
        )
        return (
            argv,
            (
                CliReproducibility.BEST_EFFORT
                if stochastic
                else CliReproducibility.EXACT_INPUTS_REQUIRED
            ),
            warnings,
        )

    @staticmethod
    def _generate(
        request: CliGenerateRequest,
    ) -> tuple[list[str], CliReproducibility, tuple[str, ...]]:
        argv = [
            "corpusgen",
            "generate",
            "--backend",
            request.backend.value,
            "--language",
            request.language,
        ]
        CliParityService._append_generation_source(argv, request)
        argv.extend(("--target", request.target_source))
        if request.phonemes:
            argv.extend(("--phonemes", ",".join(request.phonemes)))
        if request.weights:
            argv.extend(
                (
                    "--weights",
                    ",".join(f"{item.unit}:{item.weight}" for item in request.weights),
                )
            )
        argv.extend(
            (
                "--unit",
                request.unit.value,
                "--target-coverage",
                str(request.target_coverage),
                "--candidates",
                str(request.candidates_per_iteration),
            )
        )
        if request.max_sentences is not None:
            argv.extend(("--max-sentences", str(request.max_sentences)))
        if request.max_iterations is not None:
            argv.extend(("--max-iterations", str(request.max_iterations)))
        if request.timeout_seconds is not None:
            argv.extend(("--timeout", str(request.timeout_seconds)))
        CliParityService._append_backend_options(argv, request)
        CliParityService._append_guidance_options(argv, request)
        CliParityService._append_scorer_options(argv, request)
        argv.extend(("--format", request.output_format.value))
        if request.output_path is not None:
            argv.extend(("--output", request.output_path))

        warnings = [
            "Generation always resolves a PHOIBLE baseline; --phonemes adds symbols rather "
            "than replacing that inventory.",
            "The CLI does not expose CorpusKit's durable execution, immutable artifact, "
            "tenant, quota, or provider-confirmation controls.",
            "The CLI JSON omits provider and model identity, per-candidate phonemes, source "
            "IDs, coverage gains, usage, and execution manifests; only accepted text and "
            "aggregate loop metrics can be compared.",
            "The CLI does not expose CorpusKit's readability scorer or readability filter.",
        ]
        if request.dataset is not None:
            warnings.append(
                "The CorpusGen CLI cannot pin a dataset config or commit revision; use the "
                "CorpusKit repository worker for reproducible remote imports."
            )
        if request.backend is CliGenerationBackend.LLM_API:
            warnings.append(
                "Provider credentials must come from the caller's environment and are never "
                "included in a preview. External provider output is not reproducible."
            )
            warnings.append(
                "The CLI infers a provider only from the model namespace and does not expose "
                "CorpusKit's separate provider allowlist, connection identity, request pacing, "
                "retry, budget, or usage controls."
            )
            reproducibility = CliReproducibility.EXTERNAL_DEPENDENCY
        elif request.backend is CliGenerationBackend.LOCAL:
            warnings.append(
                "The CLI accepts a mutable model identifier or path and does not expose "
                "CorpusKit's verified offline revision and snapshot digest. It also has no "
                "seed, top-p, or sampling-mode flags; output is best-effort reproducible."
            )
            reproducibility = CliReproducibility.BEST_EFFORT
        else:
            reproducibility = CliReproducibility.EXACT_INPUTS_REQUIRED
        return argv, reproducibility, tuple(warnings)

    @staticmethod
    def _append_generation_source(argv: list[str], request: CliGenerateRequest) -> None:
        if request.file_path is not None:
            argv.extend(("--file", request.file_path))
        if request.dataset is not None:
            argv.extend(("--dataset", request.dataset, "--text-column", request.text_column))
            if request.split is not None:
                argv.extend(("--split", request.split))
            if request.max_samples is not None:
                argv.extend(("--max-samples", str(request.max_samples)))
        if request.model is not None:
            argv.extend(("--model", request.model))

    @staticmethod
    def _append_backend_options(argv: list[str], request: CliGenerateRequest) -> None:
        if request.backend is CliGenerationBackend.LLM_API:
            argv.extend(
                (
                    "--llm-temperature",
                    str(request.llm_temperature),
                    "--llm-max-tokens",
                    str(request.llm_max_tokens),
                )
            )
        elif request.backend is CliGenerationBackend.LOCAL:
            argv.extend(
                (
                    "--local-temperature",
                    str(request.local_temperature),
                    "--local-max-tokens",
                    str(request.local_max_tokens),
                    "--device",
                    request.device.value,
                    "--quantization",
                    request.quantization.value,
                )
            )
        if request.prompt_template is not None:
            argv.extend(("--prompt-template", request.prompt_template))

    @staticmethod
    def _append_guidance_options(argv: list[str], request: CliGenerateRequest) -> None:
        argv.extend(("--guidance", request.guidance.value))
        if request.guidance_config_path is not None:
            argv.extend(("--guidance-config", request.guidance_config_path))
        if request.guidance is CliGuidance.DATG and request.guidance_config_path is None:
            argv.extend(
                (
                    "--datg-boost",
                    str(request.datg_boost),
                    "--datg-penalty",
                    str(request.datg_penalty),
                    "--datg-anti-mode",
                    request.datg_anti_mode,
                    "--datg-freq-threshold",
                    str(request.datg_frequency_threshold),
                    "--datg-batch-size",
                    str(request.datg_batch_size),
                )
            )
        if request.rl_adapter_path is not None:
            argv.extend(("--rl-adapter-path", request.rl_adapter_path))

    @staticmethod
    def _append_scorer_options(argv: list[str], request: CliGenerateRequest) -> None:
        argv.extend(
            (
                "--coverage-weight",
                str(request.coverage_weight),
                "--phonotactic-weight",
                str(request.phonotactic_weight),
                "--phonotactic-scorer",
                request.phonotactic_scorer.value,
                "--phonotactic-n",
                str(request.phonotactic_n),
                "--fluency-weight",
                str(request.fluency_weight),
                "--fluency-scorer",
                request.fluency_scorer.value,
                "--fluency-device",
                request.fluency_device.value,
            )
        )
        if request.phonotactic_corpus_path is not None:
            argv.extend(("--phonotactic-corpus", request.phonotactic_corpus_path))
        if request.fluency_model is not None:
            argv.extend(("--fluency-model", request.fluency_model))

    @staticmethod
    def _powershell_quote(argument: str) -> str:
        return "'" + argument.replace("'", "''") + "'"


__all__ = ["CliParityService"]
