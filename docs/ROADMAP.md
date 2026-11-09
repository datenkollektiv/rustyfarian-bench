# Roadmap — rustyfarian-bench

Last updated: November 2026

Guided, rerunnable hardware bench tests for the rustyfarian ESP32 crates.
Items flow Long term → Mid term → Near term → Ready → done; Ready items have a feature doc in `docs/features/`.

```mermaid
timeline
    title rustyfarian-bench
    section Ready
        Prompt placeholders (feature-doc) : `say` and `confirm` fill in `{mac}`, `{port}`, `{chip}` and captured values such as an IP, so an attended router step names the device
    section Near term
        Result traceability : results record the consumer git revision, dirty flag and scenario hash
        Prompt placeholders v2 (feature-doc) : board-level variables in `bench.toml` for facts the log never prints, deferred until v1 ran on hardware
        ws2812 S3 bench : S3 pulse and rainbow plus C3 colours, comet and frame sweep in rustyfarian-ws2812, two chips to learn on
    section Mid term
        (empty) : add items once scoped
    section Long term
        (empty) : add direction-level ideas
```
