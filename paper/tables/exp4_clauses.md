| clause | scenario | nodes | configuration | test_metric | test_outcome | test_p_holm | test_holm_reachable | bound_metric | n_pairs | pct_diff | pct_diff_lo | pct_diff_hi | bound | limit | bound_ok | state |
| --- | --- | ---: | --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |
| plan | W1 | 200 | anneal-plan/t0=1/cooling=0.98 | steady_cost_per_hour | lower | 0.01953 | true | evictions | 10 | 37.69 | 29.26 | 47.57 | upper 95% bound < | 10 | false | fails |
| online | W1 | 200 | anneal-online/t0=0.3 | evictions | lower | 0.01562 | true | steady_cost_per_hour | 10 | 68.39 | 61.51 | 74.78 | lower 95% bound > | -1 | true | fails |
| online | W1 | 200 | anneal-online/t0=1 | evictions | lower | 0.01562 | true | steady_cost_per_hour | 10 | 68 | 62.01 | 73.85 | lower 95% bound > | -1 | true | fails |
| online | W1 | 200 | anneal-online/t0=3 | evictions | lower | 0.01562 | true | steady_cost_per_hour | 10 | 68.89 | 63 | 74.5 | lower 95% bound > | -1 | true | fails |
| plan | W4 | 200 | anneal-plan/t0=1/cooling=0.98 | steady_cost_per_hour | no significant difference | 0.3867 | true | evictions | 10 | 20.86 | 12.66 | 30.34 | upper 95% bound < | 10 | false | fails |
| online | W4 | 200 | anneal-online/t0=0.3 | evictions | no significant difference | 0.3164 | true | steady_cost_per_hour | 10 | 12.03 | 8.907 | 15.48 | lower 95% bound > | -1 | true | fails |
| online | W4 | 200 | anneal-online/t0=1 | evictions | no significant difference | 0.3867 | true | steady_cost_per_hour | 10 | 12.96 | 10.37 | 15.5 | lower 95% bound > | -1 | true | fails |
| online | W4 | 200 | anneal-online/t0=3 | evictions | higher | 0.01953 | true | steady_cost_per_hour | 10 | 10.99 | 7.545 | 15.04 | lower 95% bound > | -1 | true | holds |
