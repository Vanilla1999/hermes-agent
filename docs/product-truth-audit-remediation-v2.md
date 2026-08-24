# Product Truth audit remediation v2

This plan closes the independent benchmark-audit findings without changing benchmark thresholds or Product Truth/maturity claims.

## Gates

1. Hidden RED is accepted only when pytest exits 1 and JUnit proves at least one test failure with zero test/setup errors.
2. Report verification recomputes all stage predicates and binds each row to the authoritative manifest and Git provenance.
3. Task identity, first parent, issue digest, hidden-test digest, production surface, gold patch digest and manifest digest are evaluator-owned evidence.
4. Broad hidden suites are narrowed to a specific historical regression node when available.
5. Real-model oracle execution is not authorized from a normal repository checkout. It must receive an exact broken source snapshot without `.git` or `.product-truth`.
6. Oracle terminal execution is evaluator-owned: Docker only, network disabled, no cross-process reuse, no arbitrary volumes/forwarded host env/extra args, and a fresh HERMES_HOME with no config override.
7. The benchmark-specific isolation contract intentionally bypasses the product-level terminal config fallback and persisted-container reuse paths; those product hardening issues remain separate from Product Truth readiness because the oracle policy makes them unreachable during scored runs.
8. The existing 8-task threshold and historical gold patches are not weakened. Product Truth remains unproven and maturity remains Beta until the real-model oracle is run under the frozen isolation contract.

## Proof required before merge

- exact-head CI;
- verifier mutation tests fail closed;
- oracle isolation drift tests fail closed;
- two clean gold attempts for all eight historical tasks;
- generated report re-verifies against the authoritative manifest;
- no release, publish or production deployment.
