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

The research files these draw on, in the project's own vocabulary: the plan (`docs/research/PLAN.md`), the
preregistration (`experiments/PREREGISTRATION.md`), the full results (`experiments/RESULTS.md`), the decision log
(`experiments/RESEARCH_LOG.md`), the RFC (`designs/consolidation-pipeline.md`), and how to rebuild every number
(`paper/REPRODUCE.md`).
