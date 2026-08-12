export type CapabilityState =
  "available" | "degraded" | "unavailable" | "planned";

export interface Capability {
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly status: CapabilityState;
  readonly profile: string;
  readonly reason?: string;
  readonly version?: string;
  readonly required: boolean;
}

export interface CapabilityCatalog {
  readonly source: "api" | "fallback";
  readonly environment: string;
  readonly engineVersion: string | null;
  readonly checkedAt: string;
  readonly capabilities: readonly Capability[];
}

const fallbackCapabilities: readonly Capability[] = [
  {
    id: "inventory-g2p",
    name: "Inventory & G2P",
    description:
      "PHOIBLE inventory inspection and eSpeak grapheme-to-phoneme conversion.",
    status: "planned",
    profile: "Interactive CPU",
    reason:
      "Preview only until a connected API process reports compatible CorpusGen, eSpeak, and PHOIBLE dependencies.",
    required: false,
  },
  {
    id: "evaluation",
    name: "Coverage evaluation",
    description:
      "Phoneme, diphone, and triphone coverage with quality metrics and provenance.",
    status: "planned",
    profile: "CPU",
    reason:
      "Preview only until a connected API process reports the required CorpusGen and G2P dependencies.",
    required: false,
  },
  {
    id: "selection",
    name: "Corpus optimization",
    description:
      "Six selection algorithms with target, budget, and weighting controls.",
    status: "planned",
    profile: "Batch CPU",
    reason:
      "Preview only until core and optional optimization dependencies are connected.",
    required: false,
  },
  {
    id: "hosted-generation",
    name: "Hosted generation",
    description:
      "BYOK language-model generation with explicit cost and stopping limits.",
    status: "planned",
    profile: "External provider",
    reason:
      "Requires an isolated provider worker and user-supplied credentials.",
    required: false,
  },
  {
    id: "local-generation",
    name: "Local generation & DATG",
    description:
      "Local Hugging Face inference, perplexity scoring, and Phon-DATG guidance.",
    status: "planned",
    profile: "GPU inference",
    reason: "Requires an approved model and GPU inference worker.",
    required: false,
  },
  {
    id: "phon-rl",
    name: "Phon-RL training",
    description:
      "PPO-based phonetic reward training and versioned adapter publication.",
    status: "planned",
    profile: "GPU training",
    reason: "Requires an isolated GPU training worker and checkpoint storage.",
    required: false,
  },
];

const validStates = new Set<CapabilityState>([
  "available",
  "degraded",
  "unavailable",
  "planned",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function profileFor(id: string): string {
  if (
    id === "cuda" ||
    id === "local-model" ||
    id === "phon-datg" ||
    id === "phon-rl"
  ) {
    return "GPU worker";
  }
  if (id === "llm" || id === "repository") return "External provider";
  if (id === "optimization") return "Batch CPU";
  return "Interactive CPU";
}

function parseCapability(value: unknown): Capability | null {
  if (!isRecord(value)) return null;
  const id = value.id;
  const name = value.name ?? value.label;
  const description = value.description ?? value.detail;
  const status = value.status ?? value.state;
  const profile =
    value.profile ?? (typeof id === "string" ? profileFor(id) : null);
  const reasonCandidate = value.reason ?? value.remediation;
  const reason =
    typeof reasonCandidate === "string" ? reasonCandidate : undefined;
  const version = typeof value.version === "string" ? value.version : undefined;
  const required = value.required === true;
  if (
    typeof id !== "string" ||
    typeof name !== "string" ||
    typeof description !== "string" ||
    typeof status !== "string" ||
    !validStates.has(status as CapabilityState) ||
    typeof profile !== "string" ||
    (reason !== undefined && typeof reason !== "string")
  ) {
    return null;
  }
  return {
    id,
    name,
    description,
    status: status as CapabilityState,
    profile,
    required,
    ...(typeof reason === "string" ? { reason } : {}),
    ...(version ? { version } : {}),
  };
}

export type CapabilityHref =
  | "/advanced"
  | "/coverage"
  | "/analysis"
  | "/selection"
  | "/g2p"
  | "/evaluate"
  | "/generation"
  | "/inventory";

export interface CapabilityControl {
  readonly href: CapabilityHref;
  readonly label: string;
  readonly requirement: string;
}

const capabilityControls: Readonly<
  Record<string, readonly CapabilityControl[]>
> = {
  "corpusgen-core": [
    {
      href: "/coverage",
      label: "Coverage & Weighting Lab",
      requirement: "Required for tracking, weights, and reports.",
    },
    {
      href: "/analysis",
      label: "Analysis Lab",
      requirement: "Required for all CorpusGen analysis calculations.",
    },
    {
      href: "/selection",
      label: "Selection Studio",
      requirement: "Required for the four core selector algorithms.",
    },
  ],
  "espeak-g2p": [
    {
      href: "/g2p",
      label: "G2P Studio",
      requirement: "Required for raw-text IPA and n-gram transcription.",
    },
    {
      href: "/evaluate",
      label: "Evaluation Studio",
      requirement: "Required when evaluation inputs are raw text.",
    },
    {
      href: "/generation",
      label: "Repository preview",
      requirement: "Required for raw-text repository sources.",
    },
  ],
  phoible: [
    {
      href: "/inventory",
      label: "Inventory Explorer",
      requirement:
        "Required for inventories, features, classes, and provenance.",
    },
    {
      href: "/selection",
      label: "PHOIBLE selection targets",
      requirement: "Required only when PHOIBLE target mode is selected.",
    },
    {
      href: "/coverage",
      label: "PHOIBLE report targets",
      requirement: "Required only when reports use a PHOIBLE target.",
    },
  ],
  optimization: [
    {
      href: "/selection",
      label: "ILP and NSGA-II",
      requirement: "Required for the optional exact and Pareto selectors.",
    },
  ],
  repository: [
    {
      href: "/advanced",
      label: "Repository runtime",
      requirement:
        "Required for allowlisted Hugging Face imports and durable repository generation.",
    },
  ],
  llm: [
    {
      href: "/advanced",
      label: "Hosted model runtime",
      requirement:
        "Required for hosted validation, estimates, and durable generation.",
    },
  ],
  "local-model": [
    {
      href: "/advanced",
      label: "Local model runtime",
      requirement:
        "Required for offline local validation and durable inference.",
    },
  ],
  cuda: [
    {
      href: "/advanced",
      label: "GPU workloads",
      requirement:
        "Required for local inference, DATG, and Phon-RL worker profiles.",
    },
  ],
  "phon-datg": [
    {
      href: "/advanced",
      label: "Phon-DATG",
      requirement:
        "Required for bounded index labs and configured durable DATG jobs.",
    },
  ],
  "phon-rl": [
    {
      href: "/advanced",
      label: "Phon-RL",
      requirement:
        "Required for bounded PPO labs and configured durable training.",
    },
  ],
};

export function controlsForCapability(
  id: string,
): readonly CapabilityControl[] {
  return capabilityControls[id] ?? [];
}

export async function fetchCapabilityCatalog(
  signal?: AbortSignal,
): Promise<CapabilityCatalog> {
  const response = await fetch("/api/v1/capabilities", {
    cache: "no-store",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok)
    throw new Error(`Capability API returned HTTP ${response.status}`);

  const value: unknown = await response.json();
  const records = isRecord(value)
    ? Array.isArray(value.checks)
      ? value.checks
      : value.capabilities
    : null;
  if (!isRecord(value) || !Array.isArray(records)) {
    throw new Error("Capability API returned an incompatible response");
  }

  const capabilities = records.map(parseCapability);
  if (capabilities.length === 0 || capabilities.some((item) => item === null)) {
    throw new Error("Capability API returned invalid capability records");
  }

  return {
    source: "api",
    environment:
      typeof value.environment === "string"
        ? value.environment
        : "Connected runtime",
    engineVersion:
      typeof value.engineVersion === "string"
        ? value.engineVersion
        : isRecord(
              records.find(
                (item) => isRecord(item) && item.id === "corpusgen-core",
              ),
            ) &&
            typeof records.find(
              (item) => isRecord(item) && item.id === "corpusgen-core",
            )?.version === "string"
          ? (records.find(
              (item) => isRecord(item) && item.id === "corpusgen-core",
            )?.version as string)
          : null,
    checkedAt:
      typeof value.checked_at === "string"
        ? value.checked_at
        : typeof value.checkedAt === "string"
          ? value.checkedAt
          : new Date().toISOString(),
    capabilities: capabilities as Capability[],
  };
}

export function getFallbackCatalog(): CapabilityCatalog {
  return {
    source: "fallback",
    environment: "UI preview",
    engineVersion: null,
    checkedAt: new Date().toISOString(),
    capabilities: fallbackCapabilities,
  };
}
