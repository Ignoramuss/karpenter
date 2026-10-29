| configuration | scenario | nodes | placement | scale_down | groups | labels | n_sets | n_differ | differing_metrics | differing_seeds |
| --- | --- | ---: | --- | --- | --- | --- | ---: | ---: | --- | --- |
| f3ff6894cd0f6825 | W1 | 50 | least-allocated | spread | exp7-n50 + exp8 | B2 | 10 | 0 |  |  |
| 8e9a17470a20c927 | W1 | 50 | least-allocated | spread | exp7-n50 + exp8 | heuristic/entropy-greedy | 10 | 0 |  |  |
| 2ef57e42f55ef748 | W1 | 50 | least-allocated | spread | exp7-n50 + exp8 | random/entropy-greedy | 10 | 0 |  |  |
| 5024813bbe6d4505 | W1 | 200 | least-allocated | spread | abl + exp1-n200 + exp4 | no-eviction-budget + random/anneal-plan + anneal-plan/t0=1/cooling=0.98 | 10 | 0 |  |  |
| c3ae328d43046dd9 | W1 | 200 | least-allocated | spread | exp1-baselines-n200 + exp7-b2 | B2 | 10 | 0 |  |  |
| 8cba6fd6e27a7958 | W1 | 200 | least-allocated | spread | exp1-n200 + exp4 | random/entropy-greedy + entropy-greedy | 10 | 0 |  |  |
| a38d7db3c2ea43cf | W2 | 200 | least-allocated | spread | abl + exp1-n200 | no-eviction-budget + random/anneal-plan | 10 | 0 |  |  |
| 4a977b43f9c8407e | W2 | 200 | least-allocated | spread | abl + exp3 | no-annealing + merge+shift/budget-lo | 10 | 0 |  |  |
| 8cb4d62a3780c725 | W2 | 200 | least-allocated | spread | exp1-baselines-n200 + exp5 + exp6-baselines | B2 | 10 | 0 |  |  |
| 1cce45c9d87e0aab | W2 | 200 | least-allocated | spread | exp1-baselines-n200 + exp6-baselines | B3 | 10 | 0 |  |  |
| a27d3ecc8ded3db1 | W2 | 200 | least-allocated | spread | exp1-n200 + exp5 + exp6 | random/entropy-greedy + random/entropy-greedy/lambda=0.1 | 10 | 0 |  |  |
| 5674960fa8a975ec | W4 | 200 | least-allocated | spread | exp1-n200 + exp4 | random/anneal-plan + anneal-plan/t0=1/cooling=0.98 | 10 | 0 |  |  |
| 40164198cf664af6 | W4 | 200 | least-allocated | spread | exp1-n200 + exp4 | random/entropy-greedy + entropy-greedy | 10 | 0 |  |  |
| all |  |  |  |  |  |  | 130 | 0 |  |  |
