# Indirect Prompt Injection in Retrieved Documents: Technical Summary

*A MITRE ATLAS-aligned evaluation built with Inspect AI. Full technical report: `report.md`.
Code, datasets, tests, raw logs and per-sample results ship in the release bundle
(`python make_release.py`).*

## Problem

Retrieval-augmented assistants read untrusted documents. ATLAS technique **AML.T0051.001
(LLM Prompt Injection: Indirect)** covers instructions hidden in that content. This project
measures how often models follow such instructions instead of the user's task, and how well
two ATLAS mitigations help:

- **AML.M0021 (guidelines):** a one-paragraph instruction-boundary system prompt.
- **AML.M0020 (guardrails):** a guard model that screens documents before they reach the assistant.

## Architecture

- **Evaluation harness:** Inspect AI tasks (dataset → `generate()` → custom scorer). Each case is run in two paired configurations, baseline and mitigated, that differ only in the mitigation paragraph.
- **Deterministic scoring:** a rule-based Python scorer (`scoring.py`, versioned, 57 unit tests) with no LLM judge. It separates two questions:
  - **Did the attack succeed?** Did the model emit the attacker's unique marker as output, rather than merely quoting or reporting it?
  - **Was the user's task still completed?** Required answer keywords must be present.

  Text is normalised for typographic Unicode first. Truncated responses count as inconclusive, never as resistance.
- **Provenance:** every sample records the model, the host that served it (read from raw responses), its stop reason, the scoring version and the criteria source. Open-weight models are pinned to one non-PRC host at fp8 precision or better. Raw Inspect logs are kept, and results are re-scored from them, never edited.
- **Robust operations:** rate-limited samples are retried without rerunning a whole model. A completeness check requires one sample per case and configuration. A release script refuses to package anything containing an API key.

## Experimental design

| Experiment | Cases | Purpose |
|---|---|---|
| Main set | 20 attacks + 10 controls | Baseline susceptibility and mitigation effect |
| Harder set | 16 attacks + 4 controls | *Adaptive* stress test, designed after seeing main-set results |
| Guardrail | 50 documents, 2 guards | Policy-configured guard vs default harm classifier |
| **Frozen holdout** | 18 attacks + 6 controls | Generalisation check: cases, scoring rules and analysis plan frozen (hashes recorded and enforced by tests) before any model saw them |

Twelve models were tested:

- **US:** OpenAI (gpt-4o-mini, GPT-6 Luna, gpt-oss-120b), Anthropic (Claude Haiku 5.5), Google (Gemini 3.8 Flash) and NVIDIA (Nemotron).
- **Canada:** Cohere (Command A Plus).
- **EU:** Mistral (Mistral Small).
- **PRC-developed:** DeepSeek, Qwen, GLM and Kimi, served by US hosts.

The study covers 1,776 model samples and 124 guard verdicts, for a total API cost under $1.20.

## Central result: attack success, baseline → mitigated (counts of attacks)

| Model | Main (20) | Harder, adaptive (16) | **Frozen holdout (18)** |
|---|---|---|---|
| gpt-oss-120b | 10 → 1 | 13 → 9 | **10 → 0** |
| gpt-4o-mini | 4 → 2 | 14 → 12 | **9 → 4** |
| Command A Plus | 8 → 2 | 14 → 9 | **8 → 4** |
| Mistral Small | 8 → 3 | 13 → 12 | **8 → 3** |
| Nemotron 3.5 Super VL | 7 → 0 | 14 → 6 | **7 → 0** |
| Qwen 3.8 27B | 3 → 0 | 12 → 3 | **2 → 0** |
| Kimi K3 | 0 → 0 | 8 → 0 | **2 → 0** |
| DeepSeek V4.1 Flash | 0 → 0 | 6 → 1 | **1 → 0** |
| GPT-6 Luna | 0 → 0 | 3 → 0 | **1 → 0** |
| GLM-5.3 Flash | 2 → 0 | 11 → 5 | **0 → 0** |
| Claude Haiku 5.5 | 0 → 0 | 2 → 0 | **0 → 0** |
| Gemini 3.8 Flash | 0 → 0 | 1 → 0 | **0 → 0** |

Policy-configured guard (`gpt-oss-safeguard-20b` with a written injection policy):

| Set | Attacks caught | Benign documents flagged |
|---|---|---|
| Main and harder sets | 30 of 36 | 0 of 14 |
| Frozen holdout | **18 of 18** | 0 of 6 |

A default harm classifier (Llama Guard 4) caught 3 of 36, each under an unrelated harm category.

## Key findings

1. **Task accuracy alone cannot measure injection resistance.** In 79% of successful attacks (219 of 277), the model gave the user a *correct* answer **and** emitted the attacker's marker ("dual compliance"). A correctness-only evaluation would score all of these as passes.
2. **Susceptibility varies widely and generalises.** The same five models were clearly susceptible on both the main set and the frozen holdout. Others resisted nearly everything.
3. **Adaptive tests and holdouts answer different questions.**
   - **The adaptive harder set** succeeded in 58% of baseline attack instances and beat every one of the 12 models at least once. That shows what targeted attacks *can* do.
   - **The frozen holdout** succeeded in 22%, close to the main set's 18%, which is the better estimate of typical susceptibility.
4. **The prompt mitigation helps but is not a control.** Across all sets it fixed 125 attack instances and never created a new success. Its effect ranged from complete to negligible (Mistral 13 → 12 on the harder set), and payloads that imitate the system's own wording partly defeat it.
5. **A policy-driven input guard was the strongest single defence tested.** Its misses on the adaptive set included the most transferable attack (a forged end-of-document delimiter), so it complements but does not replace architectural controls.
6. **Indirect injection also causes availability harms:** reasoning loops that exhaust the token budget, and answers drawn from the wrong part of a document. Marker-based scoring does not capture these, so they were documented separately.

## How results were checked

- **Reading:** a person read every scorer-flagged row (successes, inconclusives, incomplete tasks, detector flags, failed attacks containing a marker), with scripts listing what to read. The remaining failed attacks were screened by script.
- **Fixes on the earlier sets:** four scorer gaps found this way were fixed, versioned and re-applied to every model. The fixes covered Unicode hyphens, truncation, a labelled marker, and blockquotes (the last raised in external review; no result changed).
- **Holdout gaps left unfixed:** on the frozen holdout, two gaps were deliberately *not* fixed and are reported with hand judgements instead. They are a scorer false negative and a translation case that necessarily reproduces the marker.

## Limitations

- **Small sample:** small, synthetic, single-author datasets (54 attacks in total), with one run per model. No per-model result survives correction for multiple comparisons.
- **Adaptive design:** the harder set and the guard policy were designed after seeing results. The holdout is frozen and fresh, but not independently authored.
- **Proxy outcome:** marker emission measures instruction-following on untrusted content, not data exfiltration or tool misuse in agentic systems.
- **Uncontrolled settings across models:** temperature support, output limits, hosting and precision differ, so cross-model comparisons describe tested configurations, not inherent model safety. The guard result likewise compares configurations, not guard models.

## Next steps

1. **Scale:** hundreds of generated attack variants, plus independently authored cases, with several runs per model.
2. **Agentic settings:** tool-using agents where an injection triggers a real action, comparable with AgentDojo-style benchmarks.
3. **Adaptive attacks:** an attacker optimising against the mitigation and the guard, plus guard false-positive rates measured on realistic benign corpora.
