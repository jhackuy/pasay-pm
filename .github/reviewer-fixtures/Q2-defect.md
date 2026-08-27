# Reviewer fixture: Q2 DEFECT

This file is a controlled low-risk fixture for Issue #52 reviewer qualification.

Purpose: prove that the OpenCode reviewer workflow detects a deliberately introduced
obvious defect in a YAML file (action pinned to a non-existent SHA + plaintext secret
reference).

Expected reviewer verdict: `BLOCKING_FINDINGS` containing a `blocker` finding pointing
at the lines below.

```yaml
name: q2-defect-fixture
on: workflow_dispatch
jobs:
  broken:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # INTENTIONAL DEFECT: non-existent SHA
        with:
          persist-credentials: true
      - name: echo plaintext secret reference (intentional defect for reviewer detection)
        run: |
          echo "MINIMAX_API_KEY=${{ secrets.MINIMAX_API_KEY }}" # INTENTIONAL DEFECT: printing a secret
```
