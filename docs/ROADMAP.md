# Roadmap — rustyfarian-bench

Last updated: November 2026

Guided, rerunnable hardware bench tests for the rustyfarian ESP32 crates.
Items flow Long term → Mid term → Near term → Ready → done; Ready items have a feature doc in `docs/features/`.

```mermaid
timeline
    title rustyfarian-bench
    section Ready
        Prompt placeholders (feature-doc) : `say` and `confirm` fill in `{mac}`, `{port}`, `{chip}` and captured values such as an IP, so an attended router step names the device
        Remote bench (feature-doc) : a Pi in the IoT network runs uploaded firmware and scenarios via `serve`, a devcontainer sends them with `submit --wait`; walking skeleton
        Remote bench auth (feature-doc) : bearer tokens over TLS on the `serve` API, the token reaching the devcontainer through `remoteEnv` like the Claude token
        Bench host provisioning (feature-doc) : a fresh Pi 5 becomes the bench host from git, cloud-init seed plus one Ansible push, closing both remote bench Pi gates
        Local board overrides (feature-doc) : a git-ignored `bench/bench.local.toml` sets per-machine `mac`, `usb_id`, `usb_serial` and `baud`, so committed `bench.toml` files carry no MACs (family check B08)
    section Near term
        Outbox retention : `serve` deletes finished jobs after N days, images and logs included, so stored Wi-Fi credentials and serial logs live a window, not forever
        Bench kiosk : the Pi's Waveshare 3.5" display shows the running job and the last verdicts, via a read-only job list in `serve` and a Chromium kiosk role
        Result traceability : results record the consumer git revision, dirty flag and scenario hash
        Prompt placeholders v2 (feature-doc) : board-level variables in `bench.toml` for facts the log never prints, deferred until v1 ran on hardware
        ws2812 S3 bench : S3 pulse and rainbow plus C3 colours, comet and frame sweep in rustyfarian-ws2812, two chips to learn on
    section Mid term
        Host-side board pinning : the bench host owns the MACs of its own boards, so a remote job pins a board without the client knowing it
    section Long term
        (empty) : add direction-level ideas
```
