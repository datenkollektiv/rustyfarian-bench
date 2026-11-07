# Roadmap — rustyfarian-bench

Last updated: November 2026

Guided, rerunnable hardware bench tests for the rustyfarian ESP32 crates.
Items flow Long term → Mid term → Near term → Ready → done; Ready items have a feature doc in `docs/features/`.

```mermaid
timeline
    title rustyfarian-bench
    section Ready
        Verdict reliability (feature-doc) : no false passes — `--yes` marks confirm steps unverified, a lost serial link fails watch windows, strict config, errors recorded
        Remote distribution (feature-doc) : a fresh consumer clone runs `just bench` via a pinned `uvx` install from GitHub, no rustyfarian-bench checkout needed
    section Near term
        Result traceability : results record the consumer git revision, dirty flag and scenario hash
        Cross-repo recipe : `just bench repo target` runs a sibling consumer's checks from this checkout
        Unattended and attended runs : --unattended runs every step the log can judge, --attended only the steps that need a person
        Parallel sessions : a session holds its port exclusively and detection skips ports another session holds, so two boards bench side by side
        ws2812 S3 bench : S3 pulse and rainbow plus C3 colours, comet and frame sweep in rustyfarian-ws2812, two chips to learn on
    section Mid term
        (empty) : add items once scoped
    section Long term
        (empty) : add direction-level ideas
```
