# woPlanner

Interactive race planner built on `woStrategy` data loaders and `woData` cache paths.

For local development from the sibling checkout layout:

```bash
pip install -e ../woData
pip install -e ../woStrategy
pip install -e .
woplanner
```

Use **Latest** to load the latest completed race FastF1 can find and set the
reference driver to the race winner. The plot uses an inverted y-axis so slower
cars appear lower, with start order on the left and current/result order on the
right.
