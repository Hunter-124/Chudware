"""ViewNet utility-model training.

A trial pairs a view SET (the exact shot grid the agent receives, rendered by the observer's
workers through ``shot_planner.render_shot_grid``) with the structured geometry queries of its
part (``training.viewpoint.observer.queries``); the cross-fitted observer — our own vision
network, trained from scratch — supplies P(correct) for every (set, query) pair. The utility
model (``services.viewpoint.utility.model``) learns to predict that from measured face tokens;
evaluation measures convergence and a held-out A/B scored by an independent observer decides
whether the weights ship. No language model is used anywhere.

Only ``train``/``evaluate`` (and the observer) import torch; ``trials``/``store`` measure and
store with numpy + the in-process engine.
"""
