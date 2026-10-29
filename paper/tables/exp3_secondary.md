| scenario | nodes | label | metric | n_runs | n_valid | n_invalid | mean | mean_lo | mean_hi | median | median_lo | median_hi | ref | n_pairs | n_dropped_invalid | n_undefined | n_dropped_missing | diff_mean | diff_mean_lo | diff_mean_hi | diff_median | diff_median_lo | diff_median_hi | diff_pct | diff_pct_lo | diff_pct_hi |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| W2 | 200 | merge-only/budget-lo | integrated_cost_post | 10 | 10 | 0 | 141 | 135.7 | 146.5 | 141.8 | 131.9 | 147 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W2 | 200 | merge-only/budget-lo | steady_cost_per_hour | 10 | 10 | 0 | 34.01 | 31.93 | 36.39 | 33.53 | 30.6 | 36.84 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W2 | 200 | merge-only/budget-lo | evictions | 10 | 10 | 0 | 335.1 | 292.5 | 370.1 | 328.5 | 316.5 | 384 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W2 | 200 | merge-only/budget-lo | eviction_cost | 10 | 10 | 0 | 335.1 | 292.5 | 370.1 | 328.5 | 316.5 | 384 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W2 | 200 | merge-only/budget-lo | final_nodes | 10 | 10 | 0 | 21.4 | 20.1 | 22.9 | 21 | 19 | 23.5 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W2 | 200 | merge+shift/budget-lo | integrated_cost_post | 10 | 10 | 0 | 150.8 | 144.2 | 157.4 | 152 | 141.1 | 160.9 | merge-only/budget-lo | 10 | 0 | 0 | 0 | 9.846 | 7.19 | 12.7 | 10.06 | 6.09 | 12.04 | 6.984 | 5.121 | 8.963 |
| W2 | 200 | merge+shift/budget-lo | steady_cost_per_hour | 10 | 10 | 0 | 35.76 | 32.91 | 38.68 | 35.25 | 31.58 | 39.85 | merge-only/budget-lo | 10 | 0 | 0 | 0 | 1.743 | -0.2015 | 3.9 | 1.281 | -1.227 | 3.929 | 5.125 | -0.5855 | 11.6 |
| W2 | 200 | merge+shift/budget-lo | evictions | 10 | 10 | 0 | 1144 | 1025 | 1269 | 1124 | 984 | 1300 | merge-only/budget-lo | 10 | 0 | 0 | 0 | 808.9 | 673 | 957.9 | 797.5 | 652 | 953.5 | 241.4 | 187.7 | 321.3 |
| W2 | 200 | merge+shift/budget-lo | eviction_cost | 10 | 10 | 0 | 1144 | 1025 | 1269 | 1124 | 984 | 1300 | merge-only/budget-lo | 10 | 0 | 0 | 0 | 808.9 | 673 | 957.9 | 797.5 | 652 | 953.5 | 241.4 | 187.7 | 321.3 |
| W2 | 200 | merge+shift/budget-lo | final_nodes | 10 | 10 | 0 | 21.5 | 20.1 | 23.1 | 21 | 19.5 | 23 | merge-only/budget-lo | 10 | 0 | 0 | 0 | 0.1 | -0.9 | 1.2 | -0.5 | -1.5 | 2 | 0.4673 | -4.245 | 5.584 |
| W2 | 200 | merge-only/budget-hi | integrated_cost_post | 10 | 10 | 0 | 131.6 | 127.2 | 136.3 | 131.5 | 124.3 | 137.6 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W2 | 200 | merge-only/budget-hi | steady_cost_per_hour | 10 | 10 | 0 | 26.81 | 25.69 | 27.98 | 26.77 | 25.41 | 28 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W2 | 200 | merge-only/budget-hi | evictions | 10 | 10 | 0 | 846.8 | 792.3 | 902 | 846 | 761.5 | 925 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W2 | 200 | merge-only/budget-hi | eviction_cost | 10 | 10 | 0 | 846.8 | 792.3 | 902 | 846 | 761.5 | 925 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W2 | 200 | merge-only/budget-hi | final_nodes | 10 | 10 | 0 | 17.7 | 16.6 | 18.8 | 18 | 16 | 19 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W2 | 200 | merge+shift/budget-hi | integrated_cost_post | 10 | 10 | 0 | 136.7 | 132.2 | 141.8 | 136 | 130 | 140.7 | merge-only/budget-hi | 10 | 0 | 0 | 0 | 5.089 | 3.702 | 6.572 | 4.265 | 2.89 | 7.539 | 3.868 | 2.81 | 4.978 |
| W2 | 200 | merge+shift/budget-hi | steady_cost_per_hour | 10 | 10 | 0 | 27.6 | 26.52 | 28.92 | 27.41 | 26 | 28.19 | merge-only/budget-hi | 10 | 0 | 0 | 0 | 0.7852 | 0.1099 | 1.498 | 0.5016 | -0.1214 | 1.889 | 2.928 | 0.4049 | 5.607 |
| W2 | 200 | merge+shift/budget-hi | evictions | 10 | 10 | 0 | 1039 | 958.8 | 1127 | 1031 | 917 | 1136 | merge-only/budget-hi | 10 | 0 | 0 | 0 | 192.2 | 120.4 | 261.1 | 168 | 108 | 319 | 22.7 | 13.91 | 31.23 |
| W2 | 200 | merge+shift/budget-hi | eviction_cost | 10 | 10 | 0 | 1039 | 958.8 | 1127 | 1031 | 917 | 1136 | merge-only/budget-hi | 10 | 0 | 0 | 0 | 192.2 | 120.4 | 261.1 | 168 | 108 | 319 | 22.7 | 13.91 | 31.23 |
| W2 | 200 | merge+shift/budget-hi | final_nodes | 10 | 10 | 0 | 17.8 | 17 | 18.9 | 17.5 | 17 | 18 | merge-only/budget-hi | 10 | 0 | 0 | 0 | 0.1 | -1 | 1.2 | 0 | -1.5 | 2 | 0.565 | -5.464 | 7.018 |
| W3 | 200 | merge-only/budget-lo | integrated_cost_post | 10 | 10 | 0 | 176.6 | 168.9 | 183.5 | 176.4 | 170 | 187.1 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W3 | 200 | merge-only/budget-lo | steady_cost_per_hour | 10 | 10 | 0 | 63.53 | 61.13 | 65.79 | 64.62 | 58.95 | 66.4 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W3 | 200 | merge-only/budget-lo | evictions | 10 | 10 | 0 | 1075 | 915.9 | 1245 | 1060 | 838 | 1326 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W3 | 200 | merge-only/budget-lo | eviction_cost | 10 | 10 | 0 | 1075 | 915.9 | 1245 | 1060 | 838 | 1326 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W3 | 200 | merge-only/budget-lo | final_nodes | 10 | 10 | 0 | 107.9 | 81.2 | 136.8 | 99.5 | 74 | 161 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W3 | 200 | merge+shift/budget-lo | integrated_cost_post | 10 | 10 | 0 | 179.9 | 171.2 | 187.7 | 178.7 | 172.5 | 194.6 | merge-only/budget-lo | 10 | 0 | 0 | 0 | 3.318 | 2.024 | 4.816 | 3.244 | 1.564 | 4.42 | 1.879 | 1.184 | 2.677 |
| W3 | 200 | merge+shift/budget-lo | steady_cost_per_hour | 10 | 10 | 0 | 65.39 | 62.54 | 68.08 | 65.94 | 61.48 | 68.59 | merge-only/budget-lo | 10 | 0 | 0 | 0 | 1.866 | 1.176 | 2.525 | 2.46 | 0.7473 | 2.754 | 2.937 | 1.888 | 3.913 |
| W3 | 200 | merge+shift/budget-lo | evictions | 10 | 10 | 0 | 1442 | 1204 | 1688 | 1428 | 1117 | 1828 | merge-only/budget-lo | 10 | 0 | 0 | 0 | 366.5 | 240.9 | 505.5 | 370 | 162 | 522.5 | 34.08 | 23.17 | 47.14 |
| W3 | 200 | merge+shift/budget-lo | eviction_cost | 10 | 10 | 0 | 1442 | 1204 | 1688 | 1428 | 1117 | 1828 | merge-only/budget-lo | 10 | 0 | 0 | 0 | 366.5 | 240.9 | 505.5 | 370 | 162 | 522.5 | 34.08 | 23.17 | 47.14 |
| W3 | 200 | merge+shift/budget-lo | final_nodes | 10 | 10 | 0 | 124.5 | 91 | 161.5 | 108.5 | 81.5 | 188 | merge-only/budget-lo | 10 | 0 | 0 | 0 | 16.6 | 8.9 | 25.3 | 9.5 | 6 | 30.5 | 15.38 | 10.34 | 19.57 |
| W3 | 200 | merge-only/budget-hi | integrated_cost_post | 10 | 10 | 0 | 174.1 | 166 | 181.6 | 172.4 | 167.3 | 188.6 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W3 | 200 | merge-only/budget-hi | steady_cost_per_hour | 10 | 10 | 0 | 61.03 | 58.64 | 63.36 | 61.81 | 58.15 | 63.98 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W3 | 200 | merge-only/budget-hi | evictions | 10 | 10 | 0 | 1527 | 1204 | 1865 | 1408 | 1095 | 2074 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W3 | 200 | merge-only/budget-hi | eviction_cost | 10 | 10 | 0 | 1527 | 1204 | 1865 | 1408 | 1095 | 2074 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W3 | 200 | merge-only/budget-hi | final_nodes | 10 | 10 | 0 | 89.1 | 71.5 | 108.5 | 81.5 | 64 | 114.5 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| W3 | 200 | merge+shift/budget-hi | integrated_cost_post | 10 | 10 | 0 | 177.1 | 169.2 | 184.2 | 177.2 | 170.4 | 188.4 | merge-only/budget-hi | 10 | 0 | 0 | 0 | 2.975 | 0.7316 | 5.203 | 2.91 | 0.1704 | 5.454 | 1.709 | 0.4167 | 3.032 |
| W3 | 200 | merge+shift/budget-hi | steady_cost_per_hour | 10 | 10 | 0 | 63.37 | 61.01 | 65.65 | 63.35 | 60.41 | 65.8 | merge-only/budget-hi | 10 | 0 | 0 | 0 | 2.339 | 1.288 | 3.446 | 2.417 | 0.7495 | 3.57 | 3.832 | 2.078 | 5.708 |
| W3 | 200 | merge+shift/budget-hi | evictions | 10 | 10 | 0 | 2112 | 1710 | 2604 | 1774 | 1564 | 2759 | merge-only/budget-hi | 10 | 0 | 0 | 0 | 585.5 | 330 | 906.8 | 536.5 | 288 | 711.5 | 38.34 | 20.59 | 61.44 |
| W3 | 200 | merge+shift/budget-hi | eviction_cost | 10 | 10 | 0 | 2112 | 1710 | 2604 | 1774 | 1564 | 2759 | merge-only/budget-hi | 10 | 0 | 0 | 0 | 585.5 | 330 | 906.8 | 536.5 | 288 | 711.5 | 38.34 | 20.59 | 61.44 |
| W3 | 200 | merge+shift/budget-hi | final_nodes | 10 | 10 | 0 | 104.6 | 80.6 | 131.2 | 96.5 | 73 | 143 | merge-only/budget-hi | 10 | 0 | 0 | 0 | 15.5 | 7.1 | 24.1 | 17.5 | 1.5 | 29 | 17.4 | 9.04 | 23.8 |
