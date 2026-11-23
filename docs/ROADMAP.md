# Roadmap — rustyfarian-bench

Last updated: November 2026

Guided, rerunnable hardware bench tests for the rustyfarian ESP32 crates.
Items flow Long term → Mid term → Near term → Ready → done; Ready items have a feature doc in `docs/features/`.

```mermaid
timeline
    title rustyfarian-bench
    section Ready
        Remote bench auth (feature-doc) : bearer tokens over TLS on the `serve` API, the token reaching the devcontainer through `remoteEnv` like the Claude token
        Remote flash keeps NVS (feature-doc) : `serve` writes the merged image by its partition table, so NVS survives a remote job and an uploaded NVS image provisions a device over the bench, which rgb-clock's provisioned checks need
        Remote board list (feature-doc) : `GET /boards` lists the bench's ports and names verified boards from a host-side `parts.toml`, and `POST /boards/probe` reads MACs through the job queue, so rgb-clock enrols a board without SSH
        Bench kiosk v2 (feature-doc) : the display shows the running job and the last verdicts, from a status file `serve` writes and a `kiosk` subcommand polls
    section Near term
        Bench maintenance mode : `serve` refuses new jobs while a maintenance marker exists, so `pi-upgrade` and a restart drain the queue instead of racing a submit
        Outbox retention : `serve` deletes finished jobs after N days, images and logs included, so stored Wi-Fi credentials and serial logs live a window, not forever, while the newest 5 finished jobs stay for the kiosk's verdict history
        Host-side board pinning : a remote job asks for a board by its `parts.toml` name, so the client needs no MAC; v2 of the remote board list
        Result traceability : results record the consumer git revision, dirty flag and scenario hash
        Prompt placeholders v2 (feature-doc) : board-level variables in `bench.toml` for facts the log never prints, deferred until v1 ran on hardware
        ws2812 S3 bench : S3 pulse and rainbow plus C3 colours, comet and frame sweep in rustyfarian-ws2812, two chips to learn on
    section Long term
        Rust rewrite : the bench becomes one Rust binary in a new repo, keeping the `bench.toml`, `results.jsonl` and `serve` API contract
```
