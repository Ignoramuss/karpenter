# Consolidation research: reader's guide

Three documents written for readers who know Karpenter's features but not this research branch's jargon:

- [`../../paper/paper.md`](../../paper/paper.md): the paper. What consolidation is for, from first principles; the
  pluggable pipeline; the entropy-reduction hypotheses; the simulator and cluster experiments and their results;
  what adds entropy to a fleet (scale-down spreading, scheduler scoring, churn, pod shape, and scheduling
  constraints); what to do next.
- [`pipeline-refactor.md`](pipeline-refactor.md): the refactor of consolidation into four pluggable stages, how it
  reproduces upstream's decisions, what it makes possible, what it costs, and what the experiments found when it
  was used.
- [`kwok-experiments-and-eks-plan.md`](kwok-experiments-and-eks-plan.md): the experiments that ran on a real
  control plane with KWOK nodes, every controlled variable, the results, and a staged plan for a real EKS cluster
  with costs.
- [`../../designs/consolidation-bounded-search-and-decision-log.md`](../../designs/consolidation-bounded-search-and-decision-log.md):
  the upstream RFC. What of this work is worth carrying into Karpenter itself (explicit search budgets, a decision
  log, the stage seams with upstream's own search behind a feature gate), with the evidence, the non-goals and the
  open questions a maintainer would ask.
- [`../../experiments/EXP9.md`](../../experiments/EXP9.md): the follow-up experiment on biased candidate selection,
  potential-guided shifts and early stopping, run after the paper.

The research files these draw on, in the project's own vocabulary: the plan (`docs/research/PLAN.md`), the
preregistration (`experiments/PREREGISTRATION.md`), the full results (`experiments/RESULTS.md`), the decision log
(`experiments/RESEARCH_LOG.md`), the RFC (`designs/consolidation-pipeline.md`), and how to rebuild every number
(`paper/REPRODUCE.md`).
