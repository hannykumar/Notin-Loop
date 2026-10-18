# factory_flow

Dark-factory confidence layer built on `../SRE-agent`. Spec: [PRD.md](PRD.md). Working rules: [../CLAUDE.md](../CLAUDE.md).

```bash
# from the repo root
/path/to/python3.12 -m venv .venv
.venv/bin/pip install -r factory_flow/requirements.txt
cp factory_flow/.env.example factory_flow/.env   # fill in LLM_* and AUDIT_HMAC_KEY

cd factory_flow
../.venv/bin/python -c "from factory.sre_base import canonical_hash, evaluate_evidence_gate, normalize_generic_alert, classify_verification_outcome"
```

`SRE-agent/` is a plain copy of github.com/hannykumar/SRE-agent at commit `b4f7e0c`.
