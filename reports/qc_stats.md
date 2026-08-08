# QC metric distributions

Every threshold in `config/qc.yaml` is justified against these numbers. They are read
back out of `qc_results.metrics_json` by SQL; no rule is re-run to produce them.

## Verdicts (latest per episode and rule)

| rule | verdict | episodes |
|---|---|---|
| ACTION_JERK | PASS | 142 |
| ACTION_JERK | SKIPPED | 60 |
| ACTION_RANGE | PASS | 142 |
| ACTION_RANGE | SKIPPED | 60 |
| FPS_DRIFT | SKIPPED | 202 |
| GRIPPER_STUCK | PASS | 57 |
| GRIPPER_STUCK | REVIEW | 5 |
| GRIPPER_STUCK | SKIPPED | 140 |
| POSE_COVERAGE | PASS | 20 |
| POSE_COVERAGE | SKIPPED | 182 |
| SEGMENT_BOUNDS | PASS | 59 |
| SEGMENT_BOUNDS | REVIEW | 1 |
| SEGMENT_BOUNDS | SKIPPED | 142 |
| STATE_ACTION_ECHO | PASS | 130 |
| STATE_ACTION_ECHO | SKIPPED | 72 |
| STATIC_EPISODE | PASS | 202 |
| TERMINATION_CONSISTENCY | PASS | 130 |
| TERMINATION_CONSISTENCY | SKIPPED | 72 |
| TS_MONOTONIC | SKIPPED | 202 |
| VIDEO_FRAME_MISMATCH | SKIPPED | 202 |

## Metrics

| source | rule | metric | n | min | p1 | mean | p99 | max |
|---|---|---|---|---|---|---|---|---|
| aloha_sim_insertion | ACTION_JERK | jerk_isolation | 50 | 1.61538 | 1.63341 | 3.1577 | 13.3867 | 16 |
| aloha_sim_insertion | ACTION_JERK | jerk_ratio | 50 | 1.49786 | 1.49786 | 1.95777 | 3.08301 | 3.5 |
| aloha_sim_insertion | ACTION_JERK | n_channels_examined | 50 | 14 | 14 | 14 | 14 | 14 |
| aloha_sim_insertion | ACTION_JERK | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| aloha_sim_insertion | ACTION_JERK | n_jerks | 50 | 0 | 0 | 0 | 0 | 0 |
| aloha_sim_insertion | ACTION_RANGE | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| aloha_sim_insertion | ACTION_RANGE | n_non_finite | 50 | 0 | 0 | 0 | 0 | 0 |
| aloha_sim_insertion | ACTION_RANGE | n_out_of_range | 50 | 0 | 0 | 0 | 0 | 0 |
| aloha_sim_insertion | ACTION_RANGE | n_physical_channels | 50 | 14 | 14 | 14 | 14 | 14 |
| aloha_sim_insertion | FPS_DRIFT | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| aloha_sim_insertion | GRIPPER_STUCK | min_unique_values | 50 | 46 | 46.98 | 57.14 | 68.51 | 69 |
| aloha_sim_insertion | GRIPPER_STUCK | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| aloha_sim_insertion | GRIPPER_STUCK | n_gripper_channels | 50 | 2 | 2 | 2 | 2 | 2 |
| aloha_sim_insertion | POSE_COVERAGE | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| aloha_sim_insertion | SEGMENT_BOUNDS | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| aloha_sim_insertion | STATE_ACTION_ECHO | correlation | 50 | 0.881013 | 0.89577 | 0.948542 | 0.968729 | 0.969178 |
| aloha_sim_insertion | STATE_ACTION_ECHO | echo_fraction | 50 | 0 | 0 | 0 | 0 | 0 |
| aloha_sim_insertion | STATE_ACTION_ECHO | max_abs_difference | 50 | 0.449427 | 0.449662 | 0.589187 | 0.825486 | 0.882892 |
| aloha_sim_insertion | STATE_ACTION_ECHO | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| aloha_sim_insertion | STATIC_EPISODE | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| aloha_sim_insertion | STATIC_EPISODE | still_fraction | 50 | 0 | 0 | 0 | 0 | 0 |
| aloha_sim_insertion | TERMINATION_CONSISTENCY | n_end_signals | 50 | 1 | 1 | 1 | 1 | 1 |
| aloha_sim_insertion | TERMINATION_CONSISTENCY | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| aloha_sim_insertion | TERMINATION_CONSISTENCY | n_interior_signals | 50 | 0 | 0 | 0 | 0 | 0 |
| aloha_sim_insertion | TERMINATION_CONSISTENCY | terminal_run | 50 | 1 | 1 | 1 | 1 | 1 |
| aloha_sim_insertion | TS_MONOTONIC | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| aloha_sim_insertion | VIDEO_FRAME_MISMATCH | n_frames | 50 | 500 | 500 | 500 | 500 | 500 |
| berkeley_ur5 | ACTION_JERK | jerk_isolation | 12 | 1.43023 | 1.46386 | 2.5e+08 | 1e+09 | 1e+09 |
| berkeley_ur5 | ACTION_JERK | jerk_ratio | 12 | 1.05834 | 1.06055 | 1.17822 | 1.51289 | 1.53137 |
| berkeley_ur5 | ACTION_JERK | n_channels_examined | 12 | 6 | 6 | 6.58333 | 7 | 7 |
| berkeley_ur5 | ACTION_JERK | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| berkeley_ur5 | ACTION_JERK | n_jerks | 12 | 0 | 0 | 0 | 0 | 0 |
| berkeley_ur5 | ACTION_RANGE | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| berkeley_ur5 | ACTION_RANGE | n_non_finite | 12 | 0 | 0 | 0 | 0 | 0 |
| berkeley_ur5 | ACTION_RANGE | n_out_of_range | 12 | 0 | 0 | 0 | 0 | 0 |
| berkeley_ur5 | ACTION_RANGE | n_physical_channels | 12 | 7 | 7 | 7 | 7 | 7 |
| berkeley_ur5 | FPS_DRIFT | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| berkeley_ur5 | GRIPPER_STUCK | min_gripper_travel | 12 | 0 | 0 | 1.16667 | 2 | 2 |
| berkeley_ur5 | GRIPPER_STUCK | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| berkeley_ur5 | GRIPPER_STUCK | n_gripper_channels | 12 | 1 | 1 | 1 | 1 | 1 |
| berkeley_ur5 | POSE_COVERAGE | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| berkeley_ur5 | SEGMENT_BOUNDS | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| berkeley_ur5 | STATE_ACTION_ECHO | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| berkeley_ur5 | STATIC_EPISODE | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| berkeley_ur5 | STATIC_EPISODE | still_fraction | 12 | 0 | 0 | 0 | 0 | 0 |
| berkeley_ur5 | TERMINATION_CONSISTENCY | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| berkeley_ur5 | TS_MONOTONIC | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| berkeley_ur5 | VIDEO_FRAME_MISMATCH | n_frames | 12 | 69 | 69.55 | 95 | 120.67 | 121 |
| epic100 | ACTION_JERK | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| epic100 | ACTION_RANGE | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| epic100 | FPS_DRIFT | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| epic100 | GRIPPER_STUCK | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| epic100 | POSE_COVERAGE | longest_gap_s | 20 | 0 | 0 | 0 | 0 | 0 |
| epic100 | POSE_COVERAGE | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| epic100 | POSE_COVERAGE | n_unregistered | 20 | 0 | 0 | 0 | 0 | 0 |
| epic100 | POSE_COVERAGE | pose_coverage | 20 | 1 | 1 | 1 | 1 | 1 |
| epic100 | SEGMENT_BOUNDS | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| epic100 | SEGMENT_BOUNDS | overlap_fraction | 60 | 0 | 0 | 0.0779871 | 1.20545 | 2.41463 |
| epic100 | SEGMENT_BOUNDS | overlap_next_s | 60 | 0 | 0 | 0.029 | 0.4494 | 0.65 |
| epic100 | SEGMENT_BOUNDS | overlap_prev_s | 60 | 0 | 0 | 0.0801667 | 1.6012 | 2.97 |
| epic100 | SEGMENT_BOUNDS | segment_duration_s | 60 | 0.59 | 0.5959 | 1.87433 | 9.8478 | 10.78 |
| epic100 | STATE_ACTION_ECHO | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| epic100 | STATIC_EPISODE | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| epic100 | STATIC_EPISODE | still_fraction | 20 | 0 | 0 | 0 | 0 | 0 |
| epic100 | TERMINATION_CONSISTENCY | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| epic100 | TS_MONOTONIC | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| epic100 | VIDEO_FRAME_MISMATCH | n_frames | 60 | 30 | 30.59 | 99.6667 | 493.39 | 540 |
| pusht | ACTION_JERK | jerk_isolation | 80 | 1.5 | 1.80471 | 2.5e+07 | 1e+09 | 1e+09 |
| pusht | ACTION_JERK | jerk_ratio | 80 | 1 | 1.03593 | 1.55456 | 3.48494 | 3.61672 |
| pusht | ACTION_JERK | n_channels_examined | 80 | 2 | 2 | 2 | 2 | 2 |
| pusht | ACTION_JERK | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
| pusht | ACTION_JERK | n_jerks | 80 | 0 | 0 | 0 | 0 | 0 |
| pusht | ACTION_RANGE | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
| pusht | ACTION_RANGE | n_non_finite | 80 | 0 | 0 | 0 | 0 | 0 |
| pusht | ACTION_RANGE | n_out_of_range | 80 | 0 | 0 | 0 | 0 | 0 |
| pusht | ACTION_RANGE | n_physical_channels | 80 | 2 | 2 | 2 | 2 | 2 |
| pusht | FPS_DRIFT | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
| pusht | GRIPPER_STUCK | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
| pusht | POSE_COVERAGE | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
| pusht | SEGMENT_BOUNDS | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
| pusht | STATE_ACTION_ECHO | correlation | 80 | 0.947807 | 0.947965 | 0.978946 | 0.993824 | 0.993953 |
| pusht | STATE_ACTION_ECHO | echo_fraction | 80 | 0 | 0 | 0 | 0 | 0 |
| pusht | STATE_ACTION_ECHO | max_abs_difference | 80 | 36.4317 | 36.576 | 67.7903 | 132.084 | 141.322 |
| pusht | STATE_ACTION_ECHO | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
| pusht | STATIC_EPISODE | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
| pusht | STATIC_EPISODE | still_fraction | 80 | 0 | 0 | 0 | 0 | 0 |
| pusht | STATIC_EPISODE | travel_fraction | 80 | 0.587891 | 0.669668 | 1.52268 | 2.91559 | 3.25195 |
| pusht | TERMINATION_CONSISTENCY | n_end_signals | 80 | 2 | 2 | 2 | 2 | 2 |
| pusht | TERMINATION_CONSISTENCY | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
| pusht | TERMINATION_CONSISTENCY | n_interior_signals | 80 | 0 | 0 | 0 | 0 | 0 |
| pusht | TERMINATION_CONSISTENCY | terminal_run | 80 | 2 | 2 | 2 | 2 | 2 |
| pusht | TS_MONOTONIC | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
| pusht | VIDEO_FRAME_MISMATCH | n_frames | 80 | 60 | 65.53 | 122.188 | 191.57 | 205 |
